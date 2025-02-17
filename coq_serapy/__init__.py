#!/usr/bin/env python3
##########################################################################
#
#    This file is part of Proverbot9001.
#
#    Proverbot9001 is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Proverbot9001 is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Proverbot9001.  If not, see <https://www.gnu.org/licenses/>.
#
#    Copyright 2019 Alex Sanchez-Stern and Yousef Alhessi
#
##########################################################################

import subprocess
import threading
import re
import queue
from pathlib import Path
import argparse
import sys
import signal
import functools
from dataclasses import dataclass
import contextlib

from typing import (List, Any, Optional, cast, Tuple, Union, Iterable,
                    Iterator, Pattern, Match, Dict, TYPE_CHECKING)
from tqdm import tqdm
# These dependencies is in pip, the python package manager
from pampy import match, _, TAIL

if TYPE_CHECKING:
    from sexpdata import Sexp
from sexpdata import Symbol, loads, dumps, ExpectClosingBracket
from .util import (split_by_char_outside_matching, eprint, mybarfmt,
                  hash_file, sighandler_context, unwrap, progn,
                  parseSexpOneLevel)
from .contexts import ScrapedTactic, TacticContext, Obligation, ProofContext, \
    AbstractSyntaxTree


def set_parseSexpOneLevel_fn(newfn) -> None:
    global parseSexpOneLevel
    parseSexpOneLevel = newfn


# Some Exceptions to throw when various responses come back from coq
@dataclass
class SerapiException(Exception):
    msg: Union['Sexp', str]


@dataclass
class AckError(SerapiException):
    pass


@dataclass
class CompletedError(SerapiException):
    pass


@dataclass
class CoqExn(SerapiException):
    pass


@dataclass
class BadResponse(SerapiException):
    pass


@dataclass
class NotInProof(SerapiException):
    pass


@dataclass
class ParseError(SerapiException):
    pass


@dataclass
class LexError(SerapiException):
    pass


@dataclass
class TimeoutError(SerapiException):
    pass


@dataclass
class OverflowError(SerapiException):
    pass


@dataclass
class UnrecognizedError(SerapiException):
    pass


@dataclass
class NoSuchGoalError(SerapiException):
    pass


@dataclass
class CoqAnomaly(SerapiException):
    pass


def raise_(ex):
    raise ex


@dataclass
class TacticTree:
    children: List[Union['TacticTree', Tuple[str, int]]]

    def __repr__(self) -> str:
        result = "["
        for child in self.children:
            result += repr(child)
            result += ","
        result += "]"
        return result


class TacticHistory:
    __tree: TacticTree
    __cur_subgoal_depth: int
    __subgoal_tree: List[List[Obligation]]

    def __init__(self) -> None:
        self.__tree = TacticTree([])
        self.__cur_subgoal_depth = 0
        self.__subgoal_tree = []

    def openSubgoal(self, background_subgoals: List[Obligation]) -> None:
        curTree = self.__tree
        for i in range(self.__cur_subgoal_depth):
            assert isinstance(curTree.children[-1], TacticTree)
            curTree = curTree.children[-1]
        curTree.children.append(TacticTree([]))
        self.__cur_subgoal_depth += 1

        self.__subgoal_tree.append(background_subgoals)
        pass

    def closeSubgoal(self) -> None:
        assert self.__cur_subgoal_depth > 0
        self.__cur_subgoal_depth -= 1
        self.__subgoal_tree.pop()
        pass

    def curDepth(self) -> int:
        return self.__cur_subgoal_depth

    def addTactic(self, tactic: str, sid: int) -> None:
        curTree = self.__tree
        for i in range(self.__cur_subgoal_depth):
            assert isinstance(curTree.children[-1], TacticTree)
            curTree = curTree.children[-1]
        curTree.children.append((tactic, sid))
        pass

    def removeLast(self, all_subgoals: List[Obligation]) -> None:
        assert len(self.__tree.children) > 0, \
            "Tried to remove from an empty tactic history!"
        curTree = self.__tree
        removed = None
        for i in range(self.__cur_subgoal_depth):
            assert isinstance(curTree.children[-1], TacticTree)
            curTree = curTree.children[-1]
        if len(curTree.children) == 0:
            parent = self.__tree
            for i in range(self.__cur_subgoal_depth-1):
                assert isinstance(parent.children[-1], TacticTree)
                parent = parent.children[-1]
            removed = parent.children.pop()
            self.__cur_subgoal_depth -= 1
            self.__subgoal_tree.pop()
        else:
            lastChild = curTree.children[-1]
            if isinstance(lastChild, tuple):
                removed = curTree.children.pop()
            else:
                assert isinstance(lastChild, TacticTree)
                self.__cur_subgoal_depth += 1
                self.__subgoal_tree.append(all_subgoals)
        return removed

    def getCurrentHistory(self) -> List[str]:
        def generate() -> Iterable[str]:
            curTree = self.__tree
            for i in range(self.__cur_subgoal_depth+1):
                yield from (child for child in curTree.children
                            if isinstance(child, tuple))
                if i < self.__cur_subgoal_depth:
                    assert isinstance(curTree.children[-1], TacticTree)
                    curTree = curTree.children[-1]
            pass
        return list(generate())

    def getFullHistory(self) -> List[str]:
        def generate(tree: TacticTree) -> Iterable[str]:
            for child in tree.children:
                if isinstance(child, TacticTree):
                    yield "{"
                    yield from generate(child)
                    yield "}"
                else:
                    yield child
        return list(generate(self.__tree))

    def getAllBackgroundObligations(self) -> List[Obligation]:
        return [item for lst in self.__subgoal_tree for item in reversed(lst)]

    def getNextCancelled(self) -> str:
        curTree = self.__tree
        assert len(curTree.children) > 0, \
            "Tried to cancel from an empty history"
        for i in range(self.__cur_subgoal_depth):
            assert isinstance(curTree.children[-1], TacticTree)
            curTree = curTree.children[-1]

        if len(curTree.children) == 0:
            return "{"
        elif isinstance(curTree.children[-1], TacticTree):
            return "}"
        else:
            assert isinstance(curTree.children[-1], tuple), curTree.children[-1]
            return curTree.children[-1]

    def __str__(self) -> str:
        return f"depth {self.__cur_subgoal_depth}, {repr(self.__tree)}"


# This is the class which represents a running Coq process with Serapi
# frontend. It runs its own thread to do the actual passing of
# characters back and forth from the process, so all communication is
# asynchronous unless otherwise noted.
class SerapiInstance(threading.Thread):
    # This takes three parameters: a string to use to run serapi, a
    # list of coq includes which the files we're running on will
    # expect, and a base directory
    def __init__(self, coq_command: List[str], module_path,
                 project_path: str,
                 timeout: int = 30, use_hammer: bool = False,
                 kernel_level_terms=True,
                 reset_on_cancel_fail=True,
                 log_outgoing_messages: Optional[str] = None, verbose=0, quiet=True) -> None:
        self._hist = []
        self.coq_command = coq_command
        self.module_path = Path(str(module_path))
        self.project_path = Path(project_path)
        self.reset_on_cancel_fail = reset_on_cancel_fail
        self._n_resets = 0
        # Set up some threading stuff. I'm not totally sure what
        # daemon=True does, but I think I wanted it at one time or
        # other.

        self.timeout = timeout
        self.log_outgoing_messages = log_outgoing_messages
        self.kernel_level_terms = kernel_level_terms
        self.use_hammer = use_hammer
        # Verbosity is zero until set otherwise
        self.verbose = verbose
        # Set the "extra quiet" flag (don't print on failures) to false
        self.quiet = quiet
        self._added_libs = set()

        self.init()

    def init(self):
        self.__sema = threading.Semaphore(value=0)
        threading.Thread.__init__(self, daemon=True)
        # Open a process to coq, with streams for communicating with
        # it.
        self._proc = subprocess.Popen(self.coq_command,
                                      cwd=str(self.project_path),
                                      stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
        self._fout = self._proc.stdout
        self._fin = self._proc.stdin

        # Initialize some state that we'll use to keep track of the
        # coq state. This way we don't have to do expensive queries to
        # the other process to answer simple questions.
        self.proof_context = None  # type: Optional[ProofContext]
        self.cur_state = 0
        self.tactic_history = TacticHistory()
        self._local_lemmas: List[Tuple[str, bool]] = []

        # The messages printed to the *response* buffer by the command
        self.feedbacks: List[Any] = []

        # Set up the message queue, which we'll populate with the
        # messages from serapi.
        self.message_queue = queue.Queue()  # type: queue.Queue[str]

        # Start the message queue thread
        self.start()

        # Go through the messages and throw away the initial feedback.
        self._discard_feedback()
        # Stacks for keeping track of the current lemma and module
        self.sm_stack: List[Tuple[str, bool]] = []
        # Open the top level module
        if self.module_path and self.module_path.stem not in ["Parameter", "Prop", "Type"]:
            self.run_stmt(f"Module {self.module_path.stem}.")
        # Execute the commands corresponding to include flags we were
        # passed

        self.prelude(self.module_path, self.project_path)

        # Unset Printing Notations (to get more learnable goals?)
        self._unset_printing_notations()

        self._local_lemmas_cache: Optional[List[str]] = None
        self._module_changed = True

        # Set up CoqHammer
        if self.use_hammer:
            try:
                self.init_hammer()
            except TimeoutError:
                eprint("Failed to initialize hammer!")
                raise

    def prelude(self, module_path, project_path):
        prelude = module_path
        while prelude != project_path:
            prelude = prelude.parent
            if (prelude / '_CoqProject').is_file():
                with open(prelude / "_CoqProject", 'r') as includesfile:
                    includes = includesfile.read()
                self._exec_includes(includes, str(prelude))

    @property
    def module_stack(self) -> List[str]:
        return [entry for entry, is_section in self.sm_stack
                if not is_section]

    @property
    def section_stack(self) -> List[str]:
        return [entry for entry, is_section in self.sm_stack
                if is_section]

    @property
    def local_lemmas(self) -> List[str]:
        def generate() -> Iterable[str]:
            for (lemma, is_section) in self._local_lemmas:
                if lemma.startswith(self.module_prefix):
                    yield lemma[len(self.module_prefix):].replace('\n', '')
                else:
                    yield lemma.replace('\n', '')
        if self._module_changed:
            self._local_lemmas_cache = list(generate())
            self._module_changed = False
        return unwrap(self._local_lemmas_cache)

    def _cancel_potential_local_lemmas(self, cmd: str) -> None:
        lemmas = self._lemmas_defined_by_stmt(cmd)
        is_section = "Let" in cmd
        for lemma in lemmas:
            self._local_lemmas.remove((lemma, is_section))

    def _remove_potential_local_lemmas(self, cmd: str) -> None:
        reset_match = re.match(r"Reset\s+(.*)\.", cmd)
        if reset_match:
            reseted_lemma_name = self.module_prefix + reset_match.group(1)
            for (lemma, is_section) in list(self._local_lemmas):
                if lemma == ":":
                    continue
                lemma_match = re.match(r"\s*([\w'\.]+)\s*:", lemma)
                assert lemma_match, f"{lemma} doesnt match!"
                lemma_name = lemma_match.group(1)
                if lemma_name == reseted_lemma_name:
                    self._local_lemmas.remove((lemma, is_section))
        abort_match = re.match(r"\s*Abort", cmd)
        if abort_match:
            self._local_lemmas.pop()

    def _add_potential_local_lemmas(self, cmd: str) -> None:
        lemmas = self._lemmas_defined_by_stmt(cmd)
        is_section = "Let" in cmd
        for lemma in lemmas:
            self._local_lemmas.append((lemma, is_section))
            if lemma.startswith(self.module_prefix):
                cached = lemma[len(self.module_prefix):].replace('\n', '')
            else:
                cached = lemma.replace("\n", "")
            if self._local_lemmas_cache is not None:
                self._local_lemmas_cache.append(cached)

    def _lemmas_defined_by_stmt(self, cmd: str) -> List[str]:
        cmd = kill_comments(cmd)
        normal_lemma_match = re.match(
            r"\s*(?:(?:Local|Global)\s+)?(?:" +
            "|".join(normal_lemma_starting_patterns) +
            r")\s+([\w']*)(.*)",
            cmd,
            flags=re.DOTALL)

        if normal_lemma_match:
            lemma_name = normal_lemma_match.group(1)
            binders, body = unwrap(split_by_char_outside_matching(
                r"\(", r"\)", ":", normal_lemma_match.group(2)))
            if binders.strip():
                lemma_statement = (self.module_prefix + lemma_name +
                                   " : forall " + binders + ", " + body[1:])
            else:
                lemma_statement = self.module_prefix + lemma_name + " " + body
            return [lemma_statement]

        goal_match = re.match(r"\s*(?:Goal)\s+(.*)", cmd, flags=re.DOTALL)

        if goal_match:
            return [": " + goal_match.group(1)]

        morphism_match = re.match(
            r"\s*Add\s+(?:Parametric\s+)?Morphism.*"
            r"with signature(.*)\s+as\s+(\w*)\.",
            cmd, flags=re.DOTALL)
        if morphism_match:
            return [morphism_match.group(2) + " : " + morphism_match.group(1)]

        proposition_match = re.match(r".*Inductive\s*\w+\s*:.*Prop\s*:=(.*)",
                                     cmd, flags=re.DOTALL)
        if proposition_match:
            case_matches = re.finditer(r"\|\s*(\w+\s*:[^|]*)",
                                       proposition_match.group(1))
            constructor_lemmas = [self.module_prefix + case_match.group(1)
                                  for case_match in
                                  case_matches]
            return constructor_lemmas
        obligation_match = re.match(".*Obligation", cmd, flags=re.DOTALL)
        if obligation_match:
            return [":"]

        return []

    @property
    def sm_prefix(self) -> str:
        return "".join([sm + "." for sm, is_sec in self.sm_stack])

    @property
    def module_prefix(self) -> str:
        return "".join([module + "." for module in self.module_stack])

    @property
    def cur_lemma(self) -> str:
        return self.local_lemmas[-1]

    @property
    def cur_lemma_name(self) -> str:
        match = re.match(r"\s*([\w'\.]+)\s+:.*", self.cur_lemma)
        assert match, f"Can't match {self.cur_lemma}"
        return match.group(1)

    def tactic_context(self, relevant_lemmas) -> TacticContext:
        return TacticContext(relevant_lemmas,
                             self.prev_tactics,
                             self.hypotheses,
                             self.goals)

    # Hammer prints a lot of stuff when it gets imported. Discard all of it.
    def init_hammer(self):
        self.hammer_timeout = 10
        atp_limit = 29 * self.hammer_timeout // 60
        reconstr_limit = 28 * self.hammer_timeout // 60
        crush_limit = 3 * self.hammer_timeout // 60
        eprint("Initializing hammer", guard=self.verbose >= 2)
        self.run_stmt("From Hammer Require Import Hammer.")
        self.run_stmt(f"Set Hammer ATPLimit {atp_limit}.")
        self.run_stmt(f"Set Hammer ReconstrLimit {reconstr_limit}.")
        self.run_stmt(f"Set Hammer CrushLimit {crush_limit}.")

    # Send some text to serapi, and flush the stream to make sure they
    # get it. NOT FOR EXTERNAL USE
    def _send_flush(self, cmd: str):
        assert self._fin
        eprint("SENT: " + cmd, guard=self.verbose >= 4)
        if self.log_outgoing_messages:
            with open(self.log_outgoing_messages, 'w') as f:
                print(cmd, file=f)
        try:
            self._fin.write(cmd.encode('utf-8'))
            self._fin.flush()
        except BrokenPipeError:
            raise CoqAnomaly("Coq process unexpectedly quit. Possibly running "
                             "out of memory due to too many threads?")

    def _send_acked(self, cmd: str):
        self._send_flush(cmd)
        self._get_ack()

    def _ask(self, cmd: str, complete: bool = True):
        return loads(self._ask_text(cmd, complete))

    def _ask_text(self, cmd: str, complete: bool = True, skip_feedback: bool = False):
        assert self.message_queue.empty(), self.messages
        self._send_acked(cmd)
        msg = self._get_message_text(complete, skip_feedback)
        return msg

    @property
    def messages(self):
        return [dumps(msg) for msg in list(self.message_queue.queue)]

    def get_hammer_premise_names(self, k: int) -> List[str]:
        if not self.goals:
            return []
        try:
            oldquiet = self.quiet
            self.quiet = True
            self.run_stmt(f"predict {k}.", timeout=120)
            self.quiet = oldquiet
            premise_names = self.feedbacks[3][1][3][1][3][1][1].split(", ")
            self.cancel_last()
            return premise_names
        except CoqExn:
            return []

    def get_hammer_premises(self, k: int = 10, return_sexp=False) -> List[str]:
        old_timeout = self.timeout
        self.timeout = 600
        names = self.get_hammer_premise_names(k)

        full_lines = {
            name: self.get_full_line(name) for name in names
        }
        full_lines = {k: v for k, v in full_lines.items() if v}
        self.timeout = old_timeout
        return full_lines

    def get_full_line(self, name: str, return_sexp=False) -> str:
        try:
            self._send_acked(f"(Query () (Vernac \"Check {name}.\"))")
            try:
                nextmsg = self._get_message()
            except TimeoutError:
                eprint("Timed out waiting for initial message")
            while match(normalizeMessage(nextmsg),
                        ["Feedback", [["doc_id", int], ["span_id", int],
                                        ["route", int],
                                        ["contents", "Processed"]]],
                        lambda *args: True,
                        _,
                        lambda *args: False):
                try:
                    nextmsg = self._get_message()
                except TimeoutError:
                    eprint("Timed out waiting for message")

            coqexn_msg = match(
                normalizeMessage(nextmsg),
                ['Answer', int, ['CoqExn', TAIL]],
                lambda sentence_num, rest:
                "\n".join(searchStrsInMsg(rest)),
                str, lambda s: s,
                [str], lambda s: s,
                _, None
            )

            if coqexn_msg:
                self._get_completed()
                raise CoqExn(coqexn_msg)

            pp_term = nextmsg[1][3][1][3][1]
            try:
                nextmsg = self._get_message()
            except TimeoutError:
                eprint("Timed out waiting for message")
            match(normalizeMessage(nextmsg),
                    ["Answer", int, ["ObjList", []]],
                    lambda *args: None,
                    _, lambda *args: raise_(UnrecognizedError(nextmsg)))
            try:
                self._get_completed()
            except TimeoutError:
                eprint("Timed out waiting for completed message")

            try:
                full_line = re.sub(r"\s+", " ", self._ppToTermStr(pp_term))
            except TimeoutError:
                eprint("Timed out when converting ppterm")
                return None

            result = full_line
            if return_sexp:
                result = (full_line, pp_term)
            return result
        except TimeoutError:
            eprint("Timed out when getting full line!")
            return None

    # Run a command. This is the main api function for this
    # class. Sends a single command to the running serapi
    # instance. Returns nothing: if you want a response, call one of
    # the other methods to get it.
    def run_stmt(self, stmt: str, timeout: Optional[int] = None, return_sexp=False):
        if timeout:
            old_timeout = self.timeout
            self.timeout = timeout
        self._flush_queue()
        eprint("Running statement: " + stmt.lstrip('\n'),
               guard=self.verbose >= 2)  # lstrip makes output shorter

        self._hist.append([stmt, None, -1])

        # We need to escape some stuff so that it doesn't get stripped
        # too early.
        stmt = stmt.replace("\\", "\\\\")
        stmt = stmt.replace("\"", "\\\"")
        # Kill the comments early so we can recognize comments earlier
        stmt = kill_comments(stmt)
        # We'll wrap the actual running in a try block so that we can
        # report which command the error came from at this
        # level. Other higher level code might re-catch it.
        context_before = self.proof_context
        # history_len_before = len(self.tactic_history.getFullHistory())
        try:
            # Preprocess_command sometimes turns one command into two,
            # to get around some limitations of the serapi interface.
            for stm in preprocess_command(stmt):
                self._add_potential_module_stack_cmd(stm)
                # Get initial context
                # Send the command
                assert self.message_queue.empty(), self.messages
                self._send_acked("(Add () \"{}\")\n".format(stm))

                self.feedbacks = []

                # Get the response, which indicates what state we put
                # serapi in.
                self._update_state()
                self._get_completed()
                assert self.message_queue.empty()

                # TODO: only for hammer tactic
                if not 'hammer.' in stm:
                    self.feedbacks = []

                # Track goal opening/closing
                is_goal_open = re.match(r"\s*(?:\d+\s*:)?\s*[{]\s*", stm)
                is_goal_close = re.match(r"\s*[}]\s*", stm)
                is_unshelve = re.match(r"\s*Unshelve\s*\.\s*", stm)

                if return_sexp:
                    ast = loads(self._ask_text(f'(Parse () "{stm}")'))[2][1][0]

                # Execute the statement.
                self._send_acked("(Exec {})\n".format(self.cur_state))

                self._hist[-1][1] = False

                # Finally, get the result of the command
                self.feedbacks.extend(self._get_feedbacks())
                # Get a new proof context, if it exists
                if is_goal_open:
                    self._get_enter_goal_context()
                elif is_goal_close or is_unshelve:
                    self._get_proof_context(update_nonfg_goals=True)
                else:
                    self._get_proof_context(update_nonfg_goals=False)

                if not context_before and self.proof_context:
                    self._add_potential_local_lemmas(stm)
                elif not self.proof_context:
                    self._remove_potential_local_lemmas(stm)
                    self.tactic_history = TacticHistory()

                # Manage the tactic history
                if possibly_starting_proof(stm) and self.proof_context:
                    self.tactic_history.addTactic(stm, self.cur_state)
                elif is_goal_open:
                    assert context_before
                    self.tactic_history.openSubgoal(
                        context_before.fg_goals[1:])
                elif is_goal_close:
                    self.tactic_history.closeSubgoal()
                elif self.proof_context:
                    # If we saw a new proof context, we're still in a
                    # proof so append the command to our prev_tactics
                    # list.
                    self.tactic_history.addTactic(stm, self.cur_state)

                if return_sexp and self.proof_context and self.proof_context.fg_goals:
                    ast = dumps(self.proof_context.fg_goals[0].goal.ast)

                self._hist[-1][1] = True
                self._hist[-1][2] = self.cur_state

        # If we hit a problem let the user know what file it was in,
        # and then throw it again for other handlers. NOTE: We may
        # want to make this printing togglable (at this level), since
        # sometimes errors are expected.
        except (CoqExn, BadResponse, AckError,
                CompletedError, TimeoutError) as e:
            self._handle_exception(e, stmt)
        except CoqAnomaly as e:
            if 'timing out' in str(e).lower() and not self._hist[-1][1]:
                self._hist = self._hist[:-1]
                self.reset()
                raise CoqAnomaly('Goal query (probably) timed out. Resetting coq.')
        finally:
            if self.proof_context and self.verbose >= 3:
                eprint(
                    f"History is now {self.tactic_history.getFullHistory()}")
                summarizeContext(self.proof_context)
            if timeout:
                self.timeout = old_timeout

        if return_sexp:
            return ast

    @property
    def prev_tactics(self):

        return self.tactic_history.getCurrentHistory()

    def _handle_exception(self, e: SerapiException, stmt: str):
        if self._hist[-1][1] is None or not self._hist[-1][1]:
            self._hist = self._hist[:-1]

        eprint("Problem running statement: {}\n".format(stmt),
               guard=(not self.quiet or self.verbose >= 2))
        match(e,
              TimeoutError,
              lambda *args: progn(self.cancel_failed(),  # type: ignore
                                  raise_(TimeoutError(
                                      "Statment \"{}\" timed out."
                                      .format(stmt)))),
              _, lambda e: None)
        coqexn_msg = match(normalizeMessage(e.msg),
                           ['Answer', int, ['CoqExn', TAIL]],
                           lambda sentence_num, rest:
                           "\n".join(searchStrsInMsg(rest)),
                           str, lambda s: s,
                           [str], lambda s: s,
                           _, None)
        if coqexn_msg:
            eprint(coqexn_msg, guard=(not self.quiet or self.verbose >= 2))
            if ("Stream\\.Error" in coqexn_msg
                    or "Syntax error" in coqexn_msg
                    or "Syntax Error" in coqexn_msg):
                self._get_completed()
                self.cur_state = self.prev_state
                raise ParseError(f"Couldn't parse command {stmt}")
            elif "CLexer.Error" in coqexn_msg:
                self._get_completed()
                raise ParseError(f"Couldn't parse command {stmt}")
            elif "NoSuchGoals" in coqexn_msg:
                self._get_completed()
                self.cancel_failed()
                raise NoSuchGoalError("")
            elif "Invalid_argument" in coqexn_msg:
                raise ParseError(f"Invalid argument in {stmt}")
            elif "Not_found" in coqexn_msg:
                self._get_completed()
                self.cancel_failed()
                raise e
            elif "Overflowed" in coqexn_msg or "Stack overflow" in coqexn_msg:
                self._get_completed()
                raise CoqAnomaly("Overflowed")
            elif "Anomaly" in coqexn_msg:
                self._get_completed()
                raise CoqAnomaly(coqexn_msg)
            elif "Unable to unify" in coqexn_msg:
                self._get_completed()
                self.cancel_failed()
                raise CoqExn(coqexn_msg)
            elif re.match(r".*The identifier (.*) is reserved\..*",
                          coqexn_msg):
                self._get_completed()
                raise CoqExn(coqexn_msg)
            else:
                self._get_completed()
                self.cancel_failed()
                raise CoqExn(coqexn_msg)
        else:
            match(normalizeMessage(e.msg),
                  ['Stream\\.Error', str],
                  lambda *args: progn(self._get_completed(),
                                      raise_(ParseError(
                                          "Couldn't parse command {}"
                                          .format(stmt)))),

                  ['CErrors\\.UserError', _],
                  lambda inner: progn(self._get_completed(),
                                      self.cancel_failed(),  # type: ignore
                                      raise_(e)),
                  ['ExplainErr\\.EvaluatedError', TAIL],
                  lambda inner: progn(self._get_completed(),
                                      self.cancel_failed(),  # type: ignore
                                      raise_(e)),
                  _, lambda *args: progn(raise_(UnrecognizedError(args))))

    def check_term(self, term: str) -> str:
        self._send_acked(f"(Query () (Vernac \"Check {term}.\"))")
        match(normalizeMessage(self._get_message()),
              ["Feedback", [["doc_id", int],
                            ["span_id", int],
                            ["route", int],
                            ["contents", "Processed"]]],
              lambda *rest: True,
              _,
              lambda msg: raise_(UnrecognizedError(msg)))
        result = match(normalizeMessage(self._get_message()),
                       ["Feedback", [["doc_id", int],
                                     ["span_id", int],
                                     ["route", int],
                                     ["contents", _]]],
                       lambda d, s, r, contents:
                       searchStrsInMsg(contents)[0],
                       _,
                       lambda msg: raise_(UnrecognizedError(msg)))
        match(normalizeMessage(self._get_message()),
              ["Answer", int, ["ObjList", []]],
              lambda *args: True,
              _,
              lambda msg: raise_(UnrecognizedError(msg)))
        self._get_completed()
        return result

    # Flush all messages in the message queue
    def _flush_queue(self) -> None:
        while not self.message_queue.empty():
            self._get_message()

    def _ppStrToTermStr(self, pp_str: str) -> str:
        answer = self._ask(
            f"(Print ((pp ((pp_format PpStr)))) (CoqPp {pp_str}))")
        return match(normalizeMessage(answer),
                     ["Answer", int, ["ObjList", [["CoqString", _]]]],
                     lambda statenum, s: str(s),
                     ["Answer", int, ["CoqExn", TAIL]],
                     lambda statenum, msg:
                     raise_(CoqExn(searchStrsInMsg(msg))))

    def _ppToTermStr(self, pp) -> str:
        return self._ppStrToTermStr(dumps(pp))

    @functools.lru_cache(maxsize=128)
    def _sexpStrToTermStr(self, sexp_str: str) -> str:
        if self.kernel_level_terms:
            serapi_command = f"(Print ((pp ((pp_format PpStr)))) (CoqConstr {sexp_str}))"
        else:
            serapi_command = f"(Print ((pp ((pp_format PpStr)))) (CoqExpr {sexp_str}))"
        try:
            answer = self._ask(serapi_command)
            return match(normalizeMessage(answer),
                         ["Answer", int, ["ObjList", [["CoqString", _]]]],
                         lambda statenum, s: str(s),
                         ["Answer", int, ["CoqExn", TAIL]],
                         lambda statenum, msg:
                         raise_(CoqExn(searchStrsInMsg(msg))))
        except CoqExn as e:
            eprint("Coq exception when trying to convert to string:\n"
                   f"{sexp_str}", guard=self.verbose >= 1)
            eprint(e, guard=self.verbose >= 2)
            raise

    def _sexpToTermStr(self, sexp) -> str:
        return self._sexpStrToTermStr(dumps(sexp))

    def _parseSexpHypStr(self, sexp_str: str) -> str:
        var_sexps_str, mid_str, term_sexp_str = \
            cast(List[str], parseSexpOneLevel(sexp_str))

        def get_id(var_pair_str: str) -> str:
            id_possibly_quoted = unwrap(
                id_regex.match(var_pair_str)).group(1)
            if id_possibly_quoted[0] == "\"" and \
               id_possibly_quoted[-1] == "\"":
                return id_possibly_quoted[1:-1]
            return id_possibly_quoted
        ids_str = ",".join([get_id(var_pair_str) for
                            var_pair_str in
                            cast(List[str], parseSexpOneLevel(var_sexps_str))])
        term_str = self._sexpStrToTermStr(term_sexp_str)
        return f"{ids_str} : {term_str}"

    def _parseSexpHyp(self, sexp) -> str:
        var_sexps, _, term_sexp = sexp
        ids_str = ",".join([dumps(var_sexp[1]) for var_sexp in var_sexps])
        term_str = self._sexpToTermStr(term_sexp)
        return f"{ids_str} : {term_str}"

    def _parseSexpGoalStr(self, sexp_str: str) -> Obligation:
        goal_match = goal_regex.fullmatch(sexp_str)
        assert goal_match, sexp_str + "didn't match"
        goal_num_str, goal_term_str, hyps_list_str = \
            goal_match.group(1, 2, 3)
        goal = AbstractSyntaxTree(
            goal_term_str, self._sexpStrToTermStr(goal_term_str).replace(r"\.", ".")
        )
        hyps = [
            AbstractSyntaxTree(hyp_str, self._parseSexpHypStr(hyp_str))
            for hyp_str in cast(List[str], parseSexpOneLevel(hyps_list_str))
        ]
        return Obligation(hyps, goal)

    def _parseSexpGoal(self, sexp) -> Obligation:
        goal_num, goal_term, hyps_list = \
            match(normalizeMessage(sexp),
                  [["name", int], ["ty", _], ["hyp", list]],
                  lambda *args: args)
        goal = AbstractSyntaxTree(dumps(goal_term), self._sexpToTermStr(goal_term))
        hyps = [
            AbstractSyntaxTree(dumps(hyp_sexp), self._parseSexpHyp(hyp_sexp))
            for hyp_sexp in hyps_list
        ]
        return Obligation(hyps, goal)

    def _parseBgGoal(self, sexp) -> Obligation:
        return match(normalizeMessage(sexp),
                     [[], [_]],
                     lambda inner_sexp: self._parseSexpGoal(inner_sexp))

    def query_qualid(self, qualid):
        msg = loads(self._ask_text(f'(Query () (Locate "{qualid}"))'))[2][1]
        if msg == [] and qualid.startswith("SerTop."):
            qualid = qualid[len("SerTop.") :]
            msg = loads(self._ask_text(f'(Query () (Locate "{qualid}"))'))[2][1]

        if len(msg) == 1:
            short_responses = msg[0][1][0][1]
            assert str(short_responses[1][0]) == "DirPath"
            short_ident = ".".join(
                [str(x[1]) for x in short_responses[1][1][::-1]]
                + [str(short_responses[2][1])]
            )
        elif len(msg) == 0:
            short_ident = qualid
        else:
            raise ValueError(f"Something wrong with the qualid '{qualid}'.")

        return short_ident

    def query_env(self, module_path, cache=None):
        msg = self._ask_text("(Query () Env)")
        env = loads(msg, true='True_')[2][1][0]
        # store the constants
        constants = []
        for const in tqdm(env[1][0][1][0][1]):
            # identifier
            qualid = f"{print_mod_path(const[0][1])}.{const[0][2][1]}"
            if cache and qualid in cache['constants']:
                continue

            # if qualid.startswith('SerTop.'):
            #     physical_path = str(module_path)
            # else:
            #     physical_path = None  # should be implemented using (Query () LocateLibrary)

            short_ident = self.query_qualid(qualid)

            # # term
            # assert str(const[1][0][1][0]) == "const_body"
            # if str(const[1][0][1][1][0]) == "Undef":  # delaration
            #     opaque = None
            #     term = None
            # elif str(const[1][0][1][1][0]) == "Def":  # transparent definition
            #     opaque = False
            #     term = None
            # else:
            #     assert str(const[1][0][1][1][0]) == "OpaqueDef"  # opaque definition
            #     opaque = True
            #     term = None

            # type
            assert str(const[1][0][2][0]) == "const_type"
            try:
                type_sexp = dumps(const[1][0][2][1])
                type_ = self._sexpStrToTermStr(type_sexp.replace("\\'", "'"))
            except RecursionError:
                type_sexp = None,
                type_ = None

            # sort = coq._ask_text(f"(Query () (Type {type_sexp}))")
            constants.append(
                {
                    # "physical_path": physical_path,
                    "short_ident": short_ident,  # short identifier
                    "qualid": qualid,            # long identifier
                    # "term": term,                #
                    "type": type_,               # type of the constant
                    # "sort": sort,                # type of the type
                    # "opaque": opaque,            # whether constant is opaque or transparent
                    "sexp": type_sexp,
                }
            )


        # store the inductives
        inductives = []
        for induct in tqdm(env[1][0][1][1][1]):
            # identifier
            qualid = f"{print_mod_path(induct[0][1])}.{induct[0][2][1]}"

            if cache and qualid in cache['inductives']:
                continue

            short_ident = self.query_qualid(qualid)
            # if qualid.startswith("SerTop."):
            # #     logical_path = "SerTop"
            #     physical_path = str(module_path)
            # else:
            #     logical_path = mod_path_file(induct[0][1])
            #     # physical_path = os.path.relpath(self.query_library(logical_path))
            #     physical_path = None
            # # physical_path += ":" + qualid[len(logical_path) + 1 :]

            blocks = []
            for blk in induct[1][0][0][1]:
                blk_qualid = ".".join(qualid.split(".")[:-1] + [str(blk[0][1][1])])
                blk_short_ident = self.query_qualid(blk_qualid)
                # constructors
                constructors = []
                for c_name, c_type in zip(blk[3][1], blk[4][1]):
                    c_name = str(c_name[1])
                    c_type = self._sexpStrToTermStr(dumps(c_type))
                    constructors.append((c_name, c_type))
                blocks.append(
                    {
                        "short_ident": blk_short_ident,
                        "qualid": blk_qualid,
                        "constructors": constructors,
                    }
                )

            inductives.append(
                {
                    # "physical_path": physical_path,
                    "qualid": qualid,
                    "blocks": blocks,
                    # "is_record": str(induct[1][0][1][1]) != "NotRecord",
                    "sexp": dumps(induct),
                }
            )
        return constants, inductives

    def query_definition(self, name):
        """
        Try to retrieve AST (kernel-terms) for a given name of an object.
        The object should be somehow imported or processed by Coq to have
        definition

        Args:
            name : str
        Returns:
            s-expression
        """
        self._send_acked(f'(Query () (Definition "{name}"))')

        obj_list_match = None
        while obj_list_match is None:
            nextmsg = self._get_message_text()
            obj_list_match = re.match(r"\(Answer\s*\d+\(ObjList(.*)\)\)", nextmsg)
            coq_exn_match = re.match(r"\(Answer \d+\(CoqExn", nextmsg)
            if coq_exn_match:
                self._get_completed()
                return None

        self._get_completed()
        return obj_list_match.group(1)

    def query_assumptions(self, name):
        """
        Return ast of assumptions of a global:
        the terms that are defined by
            Axiom, Axioms
            Conjecture, Conjectures
            Parameter,Parameters
            Hypothesis, Hypotheses
            Variable, Variables
        """
        msg = self._ask_text(f'(Query () (Assumptions "{name}"))', skip_feedback=True)
        if re.match(r'\(Answer \d+\(CoqExn.*', msg):
            return None

        assumption = re.match(r'\(Answer \d+\(ObjList\((.*)\)\)\)\s*', msg).group(1)
        assumption = loads(assumption)[1]
        ast = None
        if assumption[2][1]:
            # variable
            ast = dumps(assumption[2][1][0])
        if assumption[3][1]:
            # axiom
            ast = dumps(assumption[3][1][0][1])
        if assumption[4][1]:
            # opaque
            ast = dumps(assumption[4][1][0])
        if assumption[5][1]:
            # trans
            ast = dumps(assumption[5][1][0][1])

        return ast

    def _query_vernac(self, cmd):
        cmd = f'(Query () (Vernac "{cmd}"))'
        self._send_acked(cmd)
        nextmsg = self._get_message()
        while match(normalizeMessage(nextmsg),
                    ["Feedback", [["doc_id", int], ["span_id", int],
                                  ["route", int],
                                  ["contents", ["ProcessingIn", str]]]],
                    lambda *args: True,
                    ["Feedback", [["doc_id", int], ["span_id", int],
                                  ["route", int],
                                  ["contents", "Processed"]]],
                    lambda *args: True,
                    _, lambda *args: False):
            nextmsg = self._get_message()
        prevmsg = nextmsg
        while match(normalizeMessage(nextmsg),
                    ['Feedback', [['doc_id', int], ['span_id', int],
                                  ['route', int],
                                  ['contents', ['Message', TAIL]]]],
                    lambda *args: True,
                    _, lambda *args: False):
            prevmsg = nextmsg
            nextmsg = self._get_message()
        self._get_completed()

        if match(normalizeMessage(prevmsg),
                 ['Answer', _, ['CoqExn', TAIL]],
                 True, _, False):
            return None
        try:
            prevmsg[1][3][1][3]
        except:
            raise CoqExn(prevmsg)

        return prevmsg[1][3][1][3]

    def locate_library(self, module):
        """
        Try to locate the physical path of the given library
        """
        cmd = f'Locate Library {module}.'
        msg = self._query_vernac(cmd)
        path = str(msg[1][2][1][-1][1])
        return path

    # Cancel the last command which was sucessfully parsed by
    # serapi. Even if the command failed after parsing, this will
    # still cancel it. You need to call this after a command that
    # fails after parsing, but not if it fails before.
    def cancel_last(self) -> None:
        cancelled = None
        if self.proof_context:
            if len(self.tactic_history.getFullHistory()) > 0:
                cancelled = self.tactic_history.getNextCancelled()
                eprint(f"Cancelling {cancelled} "
                       f"from state {self.cur_state}",
                       guard=self.verbose >= 2)
                self._cancel_potential_local_lemmas(cancelled[0])
            else:
                eprint("Cancelling something (not in history)",
                       guard=self.verbose >= 2)
        else:
            cancelled = ""
            eprint(f"Cancelling vernac "
                   f"from state {self.cur_state}",
                   guard=self.verbose >= 2)

        self.__cancel()

        if not self.proof_context:
            assert len(self.tactic_history.getFullHistory()) == 0, \
                ("History is desynced!", self.tactic_history.getFullHistory())
            self.tactic_history = TacticHistory()
        assert self.message_queue.empty(), self.messages
        if self.proof_context and self.verbose >= 3:
            eprint(f"History is now {self.tactic_history.getFullHistory()}")
            summarizeContext(self.proof_context)

        return cancelled

    def __cancel(self) -> None:
        try:
            self._flush_queue()
            assert self.message_queue.empty(), self.messages

            cancelled_state = self.cur_state
            context_before = self.proof_context

            # Run the cancel
            self._send_acked("(Cancel ({}))".format(self.cur_state))

            # Get the response from cancelling
            self.cur_state = self._get_cancelled()

            # Get a new proof context, if it exists
            self._get_proof_context()

            tactic_history = self.tactic_history.getCurrentHistory()
            if tactic_history and tactic_history[-1][1] == cancelled_state:
                self.tactic_history.removeLast(context_before.fg_goals)
            if self._hist[-1][-1] == cancelled_state:
                self._hist = self._hist[:-1]
        except Exception as e:
            if self.reset_on_cancel_fail:
                self._hist = self._hist[:-1]
                self.reset()
            else:
                raise e

    def cancel_failed(self) -> None:
        # if the last cur_state coincides with the cur state in history
        # and the last record in history is successful, then
        # we don't need to cancel anything
        if self._hist[-1][-1] == self.cur_state and self._hist[-1][1]:
            return
        self.__cancel()

    def _get_cancelled(self) -> int:
        try:
            feedback = self._get_message()

            new_statenum = \
                match(normalizeMessage(feedback),
                      ["Answer", int, ["CoqExn", TAIL]],
                      lambda docnum, rest:
                      raise_(CoqAnomaly("Overflowed"))
                      if "Stack overflow" in "\n".join(searchStrsInMsg(rest))
                      else raise_(CoqExn(feedback)),
                      ["Feedback", [['doc_id', int], ['span_id', int], TAIL]],
                      lambda docnum, statenum, *rest: statenum,
                      _, lambda *args: raise_(BadResponse(feedback)))

            cancelled_answer = self._get_message()

            cancelled_statenum = match(normalizeMessage(cancelled_answer),
                  ["Answer", int, ["Canceled", list]],
                  lambda _, statenums: min(statenums),
                  ["Answer", int, ["CoqExn", TAIL]],
                  lambda statenum, rest:
                  raise_(CoqExn("\n".join(searchStrsInMsg(rest)))),
                  _, lambda *args: raise_(BadResponse(cancelled_answer)))

        finally:
            self._get_completed()

        return new_statenum

    # Get the next message from the message queue, and make sure it's
    # an Ack
    def _get_ack(self) -> None:
        ack = self._get_message()
        match(normalizeMessage(ack),
              ["Answer", _, "Ack"], lambda state: None,
              ["Feedback", TAIL], lambda rest: self._get_ack(),
              _, lambda msg: raise_(AckError(dumps(ack))))

    # Get the next message from the message queue, and make sure it's
    # a Completed.
    def _get_completed(self) -> Any:
        completed = self._get_message()
        match(normalizeMessage(completed),
              ["Answer", int, "Completed"], lambda state: None,
              _, lambda msg: raise_(CompletedError(completed)))

    def add_lib(self, origpath: str, logicalpath: str) -> None:
        if logicalpath != '""':
            addStm = ("(Add () \"Add LoadPath \\\"{}\\\" as {}.\")\n"
                    .format(origpath, logicalpath))
        else:
            addStm = f'(Add () "Add LoadPath \\\"{origpath}\\\".")\n'

        if addStm in self._added_libs:
            return
        self._added_libs.add(addStm)

        self._send_acked(addStm)
        self._update_state()
        self._get_completed()
        self._send_acked("(Exec {})\n".format(self.cur_state))
        self._discard_feedback()
        self._discard_feedback()
        self._get_completed()

    def add_ocaml_lib(self, path: str) -> None:
        addStm = ("(Add () \"Add ML Path \\\"{}\\\".\")\n"
                  .format(path))

        if addStm in self._added_libs:
            return
        self._added_libs.add(addStm)

        self._send_acked(addStm)
        self._update_state()
        self._get_completed()
        self._send_acked("(Exec {})\n".format(self.cur_state))
        self._discard_feedback()
        self._discard_feedback()
        self._get_completed()

    def add_lib_rec(self, origpath: str, logicalpath: str) -> None:
        addStm = ("(Add () \"Add Rec LoadPath \\\"{}\\\" as {}.\")\n"
                  .format(origpath, logicalpath))
        if addStm in self._added_libs:
            return
        self._added_libs.add(addStm)
        self._send_acked(addStm)
        self._update_state()
        self._get_completed()
        self._send_acked("(Exec {})\n".format(self.cur_state))
        self._discard_feedback()
        self._discard_feedback()
        self._get_completed()

    def search_about(self, symbol: str) -> List[str]:
        self._send_acked(f"(Query () (Vernac \"Search {symbol}.\"))")
        lemma_msgs: List[str] = []
        nextmsg = self._get_message()
        while match(normalizeMessage(nextmsg),
                    ["Feedback", [["doc_id", int], ["span_id", int],
                                  ["route", int],
                                  ["contents", ["ProcessingIn", str]]]],
                    lambda *args: True,
                    ["Feedback", [["doc_id", int], ["span_id", int],
                                  ["route", int],
                                  ["contents", "Processed"]]],
                    lambda *args: True,
                    _, lambda *args: False):
            nextmsg = self._get_message()
        while match(normalizeMessage(nextmsg),
                    ["Feedback", [["doc_id", int], ["span_id", int],
                                  ["route", int],
                                  ["contents", ["Message", "Notice",
                                                [], TAIL]]]],
                    lambda *args: True,
                    ['Feedback', [['doc_id', int], ['span_id', int],
                                  ['route', int],
                                  ['contents', ['Message', TAIL]]]],
                    lambda *args: True,
                    _, lambda *args: False):
            oldmsg = nextmsg
            try:
                nextmsg = self._get_message()
                lemma_msgs.append(dumps(oldmsg[1][3][1][3][1]))
            except RecursionError:
                pass
        self._get_completed()
        str_lemmas = [(re.sub(r"\s+", " ", self._ppStrToTermStr(lemma_msg)),
                       lemma_msg)
                      for lemma_msg in lemma_msgs[:10]]
        return str_lemmas

    # Not adding any types here because it would require a lot of
    # casting. Will reassess when recursive types are added to mypy
    # https://github.com/python/mypy/issues/731
    def _ppSexpContent(self, content):
        if content[0] == "Feedback":
            return self._ppSexpContent(content[1][1][1][3][1][2])
        elif (content[0] == "PCData" and len(content) == 2
              and isinstance(content[1], str)):
            return content[1]
        elif (content[0] == "PCData" and len(content) == 2
              and content[1] == "."):
            return "."
        elif (content[0] == "Element" and len(content) == 2
              and isinstance(content[1], list) and
              (content[1][0] == "constr.keyword" or
               content[1][0] == "constr.type" or
               content[1][0] == "constr.variable" or
               content[1][0] == "constr.reference" or
               content[1][0] == "constr.path")):
            return dumps(content[1][2][0][1])
        elif isinstance(content[0], list):
            return "".join([self._ppSexpContent(item) for item in content])
        else:
            return dumps(content)

    def _exec_includes(self, includes_string: str, prelude: str) -> None:
        for rmatch in re.finditer(r"-R\s*(\S*)\s*(\S*)\s*", includes_string):
            self.add_lib_rec("./" + rmatch.group(1), rmatch.group(2))
        for qmatch in re.finditer(r"-Q\s*(\S*)\s*(\S*)\s*", includes_string):
            self.add_lib("./" + qmatch.group(1), qmatch.group(2))
        for imatch in re.finditer(r"-I\s*(\S*)", includes_string):
            self.add_ocaml_lib("./" + imatch.group(1))

    def _update_state(self) -> None:
        self.prev_state = self.cur_state
        self.cur_state = self._get_next_state()

    def _unset_printing_notations(self) -> None:
        self._send_acked("(Add () \"Unset Printing Notations.\")\n")
        self._update_state()
        self._get_completed()

    def _get_next_state(self) -> int:
        msg = self._get_message()
        while match(normalizeMessage(msg),
                    ["Feedback", TAIL], lambda tail: True,
                    ["Answer", int, "Completed"], lambda sidx: True,
                    _, lambda x: False):
            if str(msg[0]) == 'Feedback':
                self.feedbacks.append(msg)
            msg = self._get_message()

        return match(normalizeMessage(msg),
                    ["Answer", int, list],
                    lambda state_num, contents:
                    match(contents,
                        ["CoqExn", TAIL],
                        lambda rest:
                        raise_(CoqExn("\n".join(searchStrsInMsg(rest)))),
                        ["Added", int, TAIL],
                        lambda state_num, tail: state_num),
                    _, lambda x: raise_(BadResponse(msg)))

    def _discard_feedback(self) -> None:
        try:
            feedback_message = self._get_message()
            while feedback_message[1][3][1] != Symbol("Processed"):
                feedback_message = self._get_message()
        except TimeoutError:
            pass
        except CoqAnomaly as e:
            if e.msg != "Timing Out":
                raise

    def _discard_initial_feedback(self) -> None:
        feedback1 = self._get_message()
        feedback2 = self._get_message()
        match(normalizeMessage(feedback1), ["Feedback", TAIL],
              lambda *args: None,
              _, lambda *args: raise_(BadResponse(feedback1)))
        match(normalizeMessage(feedback2), ["Feedback", TAIL],
              lambda *args: None,
              _, lambda *args: raise_(BadResponse(feedback2)))

    def interrupt(self) -> None:
        self._proc.send_signal(signal.SIGINT)
        self._flush_queue()

    def _get_message(self, complete=False) -> Any:
        msg_text = self._get_message_text(complete=complete)
        assert msg_text != "None", msg_text
        if msg_text[0] != "(":
            eprint(f"Skipping non-sexp output {msg_text}",
                   guard=self.verbose>=3)
            return self._get_message(complete=complete)
        try:
            if '[)' in msg_text:  # TODO: why this happens?
                msg_text = msg_text.replace('[)', '"[")')
            msg_text = msg_text.replace("(Pp_string [)", '(Pp_string "[")')
            msg_text = msg_text.replace("(Pp_string ])", '(Pp_string "]")')
            # print(msg_text)
            return loads(msg_text, nil=None)
        except ExpectClosingBracket:
            eprint(
                f"Tried to load a message but it's ill formed! \"{msg_text}\"",
                guard=self.verbose)
            raise CoqAnomaly("")
        except AssertionError:
            eprint(f"Assertion error while parsing s-expr {msg_text}")
            raise CoqAnomaly("")

    def _get_message_text(self, complete=False, skip_feedback=False) -> Any:
        try:
            msg = self.message_queue.get(timeout=self.timeout)
            if skip_feedback:
                while msg.startswith('(Feedback'):
                    msg = self.message_queue.get(timeout=self.timeout)
            if complete:
                self._get_completed()
            assert msg is not None
            return msg
        except queue.Empty:
            eprint("Command timed out! Interrupting", guard=self.verbose)
            self._proc.send_signal(signal.SIGINT)
            num_breaks = 1
            try:
                interrupt_response = \
                    loads(self.message_queue.get(timeout=self.timeout))
            except queue.Empty:
                self._proc.send_signal(signal.SIGINT)
                num_breaks += 1
                try:
                    interrupt_response = \
                        loads(self.message_queue.get(timeout=self.timeout))
                except queue.Empty:
                    raise CoqAnomaly("Timing Out")

            got_answer_after_interrupt = match(
                normalizeMessage(interrupt_response),
                ["Answer", int, ["CoqExn", TAIL]],
                lambda *args: False,
                ["Answer", TAIL],
                lambda *args: True,
                _, lambda *args: False)
            if got_answer_after_interrupt:
                self._get_completed()
                for i in range(num_breaks):
                    try:
                        after_interrupt_msg = loads(self.message_queue.get(
                            timeout=self.timeout))
                    except queue.Empty:
                        raise CoqAnomaly("Timing out")
                    assert isBreakMessage(after_interrupt_msg), \
                        after_interrupt_msg
                assert self.message_queue.empty(), self.messages
                return dumps(interrupt_response)
            else:
                for i in range(num_breaks):
                    try:
                        after_interrupt_msg = loads(self.message_queue.get(
                            timeout=self.timeout))
                    except queue.Empty:
                        raise CoqAnomaly("Timing out")
                self._get_completed()
                assert self.message_queue.empty(), self.messages
                raise TimeoutError("")
            assert False, (interrupt_response, self.messages)

    def _get_feedbacks(self) -> List['Sexp']:
        feedbacks = []  # type: List[Sexp]
        next_message = self._get_message()
        while(isinstance(next_message, list) and
              next_message[0] == Symbol("Feedback")):
            feedbacks.append(next_message)
            next_message = self._get_message()
        fin = next_message
        match(normalizeMessage(fin),
              ["Answer", _, "Completed", TAIL], lambda *args: None,
              ['Answer', _, ["CoqExn", [_, _, _, _, _, ['str', _]]]],
              lambda statenum, loc1, loc2, loc3, loc4, loc5, inner:
              raise_(CoqExn(fin)),
              _, lambda *args: progn(eprint(f"message is \"{repr(fin)}\""),
                                     raise_(UnrecognizedError(fin))))

        return feedbacks

    def count_fg_goals(self) -> int:
        if not self.proof_context:
            return 0
        return len(self.proof_context.fg_goals)

    def _extract_proof_context(self, raw_proof_context: 'Sexp') -> str:
        assert isinstance(raw_proof_context, list), raw_proof_context
        assert len(raw_proof_context) > 0, raw_proof_context
        assert isinstance(raw_proof_context[0], list), raw_proof_context
        return cast(List[List[str]], raw_proof_context)[0][1]

    @property
    def goals(self) -> str:
        if self.proof_context and self.proof_context.fg_goals:
            return self.proof_context.fg_goals[0].goal
        else:
            return ""

    @property
    def hypotheses(self) -> List[str]:
        if self.proof_context and self.proof_context.fg_goals:
            return self.proof_context.fg_goals[0].hypotheses
        else:
            return []

    def _get_enter_goal_context(self) -> None:
        assert self.proof_context
        self.proof_context = ProofContext([self.proof_context.fg_goals[0]],
                                          self.proof_context.bg_goals +
                                          self.proof_context.fg_goals[1:],
                                          self.proof_context.shelved_goals,
                                          self.proof_context.given_up_goals)

    def _get_proof_context(self, update_nonfg_goals: bool = True) -> None:
        # Try to do this the right way, fall back to the
        # wrong way if we run into this bug:
        # https://github.com/ejgallego/coq-serapi/issues/150
        try:
            if self.kernel_level_terms:
                text_response = self._ask_text("(Query () Goals)")
                goals_match_regex = all_goals_regex
            else:
                text_response = self._ask_text("(Query () EGoals)")
                goals_match_regex = ext_goals_regex
            context_match = re.fullmatch(
                r"\(Answer\s+\d+\s*\(ObjList\s*(.*)\)\)\n",
                text_response)
            if not context_match:
                if "Stack overflow" in text_response:
                    raise CoqAnomaly(f"\"{text_response}\"")
                else:
                    raise BadResponse(f"\"{text_response}\"")
            context_str = context_match.group(1)
            if context_str == "()":
                self.proof_context = None
            else:
                goals_match = goals_match_regex.match(context_str)
                if not goals_match:
                    raise BadResponse(context_str)
                fg_goals_str, bg_goals_str, \
                    shelved_goals_str, given_up_goals_str = \
                    goals_match.groups()
                if update_nonfg_goals or self.proof_context is None:
                    unparsed_levels = cast(List[str],
                                           parseSexpOneLevel(bg_goals_str))
                    parsed2 = [uuulevel
                               for ulevel in unparsed_levels
                               for uulevel in cast(List[str],
                                                   parseSexpOneLevel(ulevel))
                               for uuulevel in
                               cast(List[str], parseSexpOneLevel(uulevel))]
                    bg_goals = [self._parseSexpGoalStr(bg_goal_str)
                                for bg_goal_str in parsed2]
                    self.proof_context = ProofContext(
                        [self._parseSexpGoalStr(goal)
                         for goal in cast(List[str],
                                          parseSexpOneLevel(fg_goals_str))],
                        bg_goals,
                        [self._parseSexpGoalStr(shelved_goal)
                         for shelved_goal in
                         cast(List[str],
                              parseSexpOneLevel(shelved_goals_str))],
                        [self._parseSexpGoalStr(given_up_goal)
                         for given_up_goal in
                         cast(List[str],
                              parseSexpOneLevel(given_up_goals_str))])
                else:
                    self.proof_context = ProofContext(
                        [self._parseSexpGoalStr(goal)
                         for goal in cast(List[str],
                                          parseSexpOneLevel(fg_goals_str))],
                        unwrap(self.proof_context).bg_goals,
                        [self._parseSexpGoalStr(shelved_goal)
                         for shelved_goal in
                         cast(List[str],
                              parseSexpOneLevel(shelved_goals_str))],
                        unwrap(self.proof_context).given_up_goals)
        except CoqExn:
            self._send_acked("(Query ((pp ((pp_format PpStr)))) Goals)")

            msg = self._get_message()
            proof_context_msg = match(
                normalizeMessage(msg),
                ["Answer", int, ["CoqExn", TAIL]],
                lambda statenum, rest:
                raise_(CoqAnomaly("Stack overflow")) if
                "Stack overflow." in searchStrsInMsg(rest) else
                raise_(CoqExn(searchStrsInMsg(rest))),
                ["Answer", int, list],
                lambda statenum, contents: contents,
                _, lambda *args:
                raise_(UnrecognizedError(dumps(msg))))
            self._get_completed()
            if len(proof_context_msg) == 0:
                self.proof_context = None
            else:
                newcontext = self._extract_proof_context(proof_context_msg[1])
                if newcontext == "none":
                    self.proof_context = ProofContext([], [], [], [])
                else:
                    self.proof_context = \
                        ProofContext(
                            [parsePPSubgoal(substr) for substr
                             in re.split(r"\n\n|(?=\snone)", newcontext)
                             if substr.strip()],
                            [], [], [])

    def get_lemmas_about_head(self) -> List[str]:
        if self.goals.str.strip() == "":
            return []
        goal_head = self.goals.str.split()[0]
        if (goal_head == "forall"):
            return []
        answer = self.search_about(goal_head)
        assert self.message_queue.empty(), self.messages
        return answer

    def run_into_next_proof(self, commands: List[str]) \
            -> Optional[Tuple[List[str], List[str]]]:
        assert not self.proof_context, "We're already in a proof"
        commands_iter = iter(commands)
        commands_run = []
        for command in commands_iter:
            self.run_stmt(command, timeout=60)
            commands_run.append(command)
            if self.proof_context:
                return list(commands_iter), commands_run
        return [], commands_run

    def finish_proof(self, commands: List[str]) \
            -> Optional[Tuple[List[str], List[str]]]:
        assert self.proof_context, "We're already out of a proof"
        commands_iter = iter(commands)
        commands_run = []
        for command in commands_iter:
            self.run_stmt(command, timeout=60)
            commands_run.append(command)
            if not self.proof_context:
                return list(commands_iter), commands_run
        return None

    def run(self) -> None:
        assert self._fout
        while not self.__sema.acquire(False):
            try:
                line = self._fout.readline().decode('utf-8')
            except ValueError:
                continue
            if line.strip() == '':
                break
            self.message_queue.put(line)
            eprint(f"RECEIVED: {line}", guard=self.verbose >= 4)

    def _add_potential_module_stack_cmd(self, cmd: str) -> None:
        new_stack = update_sm_stack(self.sm_stack, cmd)
        if len(self.sm_stack) > 0 and \
           self.sm_stack[-1][1] and \
           len(new_stack) < len(self.sm_stack):
            self._local_lemmas = \
                [(lemma, is_section) for (lemma, is_section)
                 in self._local_lemmas if not is_section]
        if len(new_stack) != len(self.sm_stack):
            self._module_changed = True
        self.sm_stack = new_stack

    def kill(self) -> None:
        assert self._proc.stdout
        self._proc.terminate()
        # try:
        #     eprint("Closing pipes")
        #     self._proc.stdout.close()
        #     eprint("Closing stdin")
        #     if self._proc.stdin:
        #         self._proc.stdin.close()
        # except BrokenPipeError:
        #     pass
        self._proc.kill()
        self.__sema.release()
        pass

    def reset(self):
        self._n_resets += 1
        hist = self._hist.copy()
        self.kill()
        self.init()
        self._hist = []
        for stm, not_failed, state in hist:
            self.run_stmt(stm)


goal_regex = re.compile(r"\(\(info\s*\(\(evar\s*\(Ser_Evar\s*(\d+)\)\)"
                        r"\(name\s*\((?:\(Id\"?\s*[\w']+\"?\))*\)\)\)\)"
                        r"\(ty\s*(.*)\)\s*\(hyp\s*(.*)\)\)")

all_goals_regex = re.compile(r"\(\(CoqGoal\s*"
                             r"\(\(goals\s*(.*)\)"
                             r"\(stack\s*(.*)\)"
                             r"\(shelf\s*(.*)\)"
                             r"\(given_up\s*(.*)\)"
                             r"\(bullet\s*.*\)\)\)\)")

ext_goals_regex = re.compile(r"\(\(CoqExtGoal\s*"
                             r"\(\(goals\s*(.*)\)"
                             r"\(stack\s*(.*)\)"
                             r"\(shelf\s*(.*)\)"
                             r"\(given_up\s*(.*)\)"
                             r"\(bullet\s*.*\)\)\)\)")

id_regex = re.compile(r"\(Id\s*(.*)\)")


def isBreakMessage(msg: 'Sexp') -> bool:
    return match(normalizeMessage(msg),
                 "Sys\\.Break", lambda *args: True,
                 _, lambda *args: False)


def isBreakAnswer(msg: 'Sexp') -> bool:
    return "Sys\\.Break" in searchStrsInMsg(normalizeMessage(msg))


@contextlib.contextmanager
def SerapiContext(*args, **kwargs) \
                  -> Iterator[Any]:
    coq = SerapiInstance(*args, **kwargs)
    try:
        yield coq
    finally:
        coq.kill()


normal_lemma_starting_patterns = [
    r"(?:Program\s+)?(?:Polymorphic\s+)?Lemma",
    "Coercion",
    r"(?:Polymorphic\s+)?Theorem",
    "Remark",
    "Proposition",
    r"(?:Polymorphic\s+)?Definition",
    "Program\s+Definition",
    "Program\s+Instance",
    "Example",
    "Fixpoint",
    # "Inductive",
    "Corollary",
    "Let",
    r"(?<!Declare\s)(?:Polymorphic\s+)?Instance",
    "Function",
    "Property",
    "Fact",
    "Equations(?:\??)"]
special_lemma_starting_patterns = [
    "Derive",
    "Goal",
    "Add Morphism",
    "Next Obligation",
    r"Obligation\s+\d+",
    "Add Parametric Morphism"]
other_starting_patterns = [
    "Functional",
    "Inductive"
]
lemma_starting_patterns = \
    normal_lemma_starting_patterns + special_lemma_starting_patterns + other_starting_patterns

assumptions_starting_patterns = [
    "Axiom", "Axioms",
    "Conjecture", "Conjectures",
    "Parameter", "Parameters",
    "Hypothesis", "Hypotheses",
    "Variable", "Variables"
]

def possibly_starting_proof(command: str) -> bool:
    stripped_command = kill_comments(command).strip()
    pattern = r"(?:(?:Local|Global)\s+)?(" + "|".join(lemma_starting_patterns) + r")\s*"
    return bool(re.match(pattern,
                         stripped_command))


def possibly_starting_term(command):
    stripped_command = kill_comments(command).strip()
    pattern = r"(?:(?:Local|Global)\s+)?(" + \
                 "|".join(lemma_starting_patterns +
                          assumptions_starting_patterns) + r")\s*"
    return bool(re.match(pattern,
                         stripped_command))


def is_proof_start(coq_commands, start_idx):
    if not possibly_starting_proof(coq_commands[start_idx]):
        return False

    # 1. find proof end,
    # 2. If in lines between start_idx and proof_end_idx there is another
    #    possibly starting proof, then the initial possibly starting proof
    #    is not a starting proof.
    i = start_idx + 1
    while i < len(coq_commands) and not ending_proof(coq_commands[i]):
        if possibly_starting_proof(coq_commands[i]):
            return False
        i += 1

    return True


def ending_proof(command: str) -> bool:
    stripped_command = kill_comments(command).strip()
    return ("Qed." in stripped_command or
            "Defined." in stripped_command or
            "Admitted." in stripped_command or
            "Abort." in stripped_command or
            "Save" in stripped_command or
            (re.match(r"\s*Proof\s+\S+\s*", stripped_command) is not None and
             re.match(r"\s*Proof\s+with", stripped_command) is None and
             re.match(r"\s*Proof\s+using", stripped_command) is None))


def initial_sm_stack(filename: str) -> List[Tuple[str, bool]]:
    return [(get_module_from_filename(filename), False)]


def update_sm_stack(sm_stack: List[Tuple[str, bool]],
                    cmd: str) -> List[Tuple[str, bool]]:
    new_stack = list(sm_stack)
    stripped_cmd = kill_comments(cmd).strip()
    module_start_match = re.match(
        r"Module\s+(?:(?:Import|Export)\s+)?(?:Type\s+)?([\w']*)", stripped_cmd)
    if stripped_cmd.count(":=") > stripped_cmd.count("with"):
        module_start_match = None
    section_start_match = re.match(r"Section\s+([\w']*)(?!.*:=)",
                                   stripped_cmd)
    end_match = re.match(r"End\s+([\w']*)\.", stripped_cmd)
    if module_start_match:
        new_stack.append((module_start_match.group(1), False))
    elif section_start_match:
        new_stack.append((section_start_match.group(1), True))
    elif end_match:
        if new_stack and new_stack[-1][0] == end_match.group(1):
            entry, is_sec = new_stack.pop()
        else:
            assert False, \
                f"Unrecognized End \"{cmd}\", " \
                f"top of module stack is {new_stack[-1]}"
    return new_stack


def module_prefix_from_stack(sm_stack: List[Tuple[str, bool]]) -> str:
    return "".join([sm[0] + "." for sm in sm_stack if not sm[1]])


def sm_prefix_from_stack(sm_stack: List[Tuple[str, bool]]) -> str:
    return "".join([sm[0] + "." for sm in sm_stack])


def kill_comments(string: str) -> str:
    result = ""
    depth = 0
    in_quote = False
    for i in range(len(string)):
        if in_quote:
            if depth == 0:
                result += string[i]
            if string[i] == '"' and string[i-1] != '\\':
                in_quote = False
        else:
            if string[i:i+2] == '(*':
                depth += 1
            if depth == 0:
                result += string[i]
            if string[i-1:i+1] == '*)' and depth > 0:
                depth -= 1
            if string[i] == '"' and string[i-1] != '\\':
                in_quote = True
    return result


def next_proof(cmds: Iterator[str]) -> Iterator[str]:
    next_cmd = next(cmds)
    assert possibly_starting_proof(next_cmd), next_cmd
    while not ending_proof(next_cmd):
        yield next_cmd
        try:
            next_cmd = next(cmds)
        except StopIteration:
            return
    yield next_cmd


def preprocess_command(cmd: str) -> List[str]:
    coq_import_match = re.fullmatch(r"\s*Require\s+Import\s+Coq\.([\w\.'])", cmd)
    if coq_import_match:
        return ["Require Import {}".format(coq_import_match.group(1))]

    return [cmd]


def get_stem(tactic: str) -> str:
    return split_tactic(tactic)[0]


def split_tactic(tactic: str) -> Tuple[str, str]:
    tactic = kill_comments(tactic).strip()
    if not tactic:
        return ("", "")
    outer_parens_match = re.fullmatch(r"\((.*)\)\.", tactic)
    if outer_parens_match:
        return split_tactic(outer_parens_match.group(1) + ".")
    if re.match(r"^\s*[-+*\{\}]+\s*$", tactic):
        stripped = tactic.strip()
        return stripped[:-1], stripped[-1]
    if split_by_char_outside_matching(r"\(", r"\)", ";", tactic):
        return tactic, ""
    for prefix in ["try", "now", "repeat", "decide"]:
        prefix_match = re.match(r"{}\s+(.*)".format(prefix), tactic)
        if prefix_match:
            rest_stem, rest_rest = split_tactic(prefix_match.group(1))
            return prefix + " " + rest_stem, rest_rest
    for special_stem in ["rewrite <-", "rewrite !",
                         "intros until", "simpl in"]:
        special_match = re.match(r"{}(:?(:?\s+(.*))|(\.))".format(special_stem), tactic)
        if special_match:
            return special_stem, special_match.group(1)
    match = re.match(r"^\(?([\w']+)(\W+.*)?", tactic)
    if not match:
        return tactic, ""
    stem, rest = match.group(1, 2)
    if not rest:
        rest = ""
    return stem, rest


def parse_hyps(hyps_str: str) -> List[str]:
    lets_killed = kill_nested(r"\Wlet\s", r"\sin\s", hyps_str)
    funs_killed = kill_nested(r"\Wfun\s", "=>", lets_killed)
    foralls_killed = kill_nested(r"\Wforall\s", ",", funs_killed)
    fixs_killed = kill_nested(r"\Wfix\s", ":=", foralls_killed)
    structs_killed = kill_nested(r"\W\{\|\s", r"\|\}", fixs_killed)
    hyps_replaced = re.sub(":=.*?:(?!=)", ":", structs_killed, flags=re.DOTALL)
    var_terms = re.findall(r"(\S+(?:, \S+)*) (?::=.*?)?:(?!=)\s.*?",
                           hyps_replaced, flags=re.DOTALL)
    if len(var_terms) == 0:
        return []
    rest_hyps_str = hyps_str
    hyps_list = []
    # Assumes hypothesis are printed in reverse order, because for
    # whatever reason they seem to be.
    for next_term in reversed(var_terms[1:]):
        next_match = rest_hyps_str.rfind(" " + next_term + " :")
        hyp = rest_hyps_str[next_match:].strip()
        rest_hyps_str = rest_hyps_str[:next_match].strip()
        hyps_list.append(hyp)
    hyps_list.append(rest_hyps_str)
    for hyp in hyps_list:
        assert re.search(":(?!=)", hyp) is not None, \
            "hyp: {}, hyps_str: {}\nhyps_list: {}\nvar_terms: {}"\
            .format(hyp, hyps_str, hyps_list, var_terms)
    return hyps_list


def kill_nested(start_string: str, end_string: str, hyps: str) \
        -> str:
    def searchpos(pattern: str, hyps: str, end: bool = False):
        match = re.search(pattern, hyps, flags=re.DOTALL)
        if match:
            if end:
                return match.end()
            else:
                return match.start()
        else:
            return float("Inf")
    next_forall_pos = searchpos(start_string, hyps)
    next_comma_pos = searchpos(end_string, hyps, end=True)
    forall_depth = 0
    last_forall_position = -1
    cur_position = 0
    while (next_forall_pos != float("Inf") or
           (next_comma_pos != float("Inf") and forall_depth > 0)):
        old_forall_depth = forall_depth
        if next_forall_pos < next_comma_pos:
            cur_position = next_forall_pos
            if forall_depth == 0:
                last_forall_position = next_forall_pos
            forall_depth += 1
        else:
            if forall_depth == 1:
                hyps = hyps[:last_forall_position] + hyps[next_comma_pos:]
                cur_position = last_forall_position
                last_forall_position = -1
            else:
                cur_position = next_comma_pos
            if forall_depth > 0:
                forall_depth -= 1

        new_next_forall_pos = \
            searchpos(start_string, hyps[cur_position+1:]) + cur_position + 1
        new_next_comma_pos = \
            searchpos(end_string, hyps[cur_position+1:], end=True) + \
            cur_position + 1
        assert new_next_forall_pos != next_forall_pos or \
            new_next_comma_pos != next_comma_pos or \
            forall_depth != old_forall_depth, \
            "old start pos was {}, new start pos is {}, old end pos was {},"\
            "new end pos is {}, cur_position is {}"\
            .format(next_forall_pos, new_next_forall_pos, next_comma_pos,
                    new_next_comma_pos, cur_position)
        next_forall_pos = new_next_forall_pos
        next_comma_pos = new_next_comma_pos
    return hyps


def get_var_term_in_hyp(hyp: str) -> str:
    return hyp.partition(":")[0].strip()


hypcolon_regex = re.compile(":(?!=)")


def get_hyp_type(hyp: str) -> str:
    splits = hypcolon_regex.split(hyp, maxsplit=1)
    if len(splits) == 1:
        return ""
    else:
        return splits[1].strip()


def get_vars_in_hyps(hyps: List[str]) -> List[str]:
    var_terms = [get_var_term_in_hyp(hyp) for hyp in hyps]
    var_names = [name.strip() for term in var_terms
                 for name in term.split(",")]
    return var_names


def get_indexed_vars_in_hyps(hyps: List[str]) -> List[Tuple[str, int]]:
    var_terms = [get_var_term_in_hyp(hyp) for hyp in hyps]
    var_names = [(name.strip(), hyp_idx)
                 for hyp_idx, term in enumerate(var_terms)
                 for name in term.split(",")]
    return var_names


def get_indexed_vars_dict(hyps: List[str]) -> Dict[str, int]:
    result = {}
    for hyp_var, hyp_idx in get_indexed_vars_in_hyps(hyps):
        if hyp_var not in result:
            result[hyp_var] = hyp_idx
    return result


def get_first_var_in_hyp(hyp: str) -> str:
    return get_var_term_in_hyp(hyp).split(",")[0].strip()


def normalizeMessage(sexp, depth: int = 5):
    if depth <= 0:
        return sexp
    if isinstance(sexp, list):
        return [normalizeMessage(item, depth=depth-1) for item in sexp]
    if isinstance(sexp, Symbol):
        return dumps(sexp)
    else:
        return sexp


def tacticTakesHypArgs(stem: str) -> bool:
    now_match = re.match(r"\s*now\s+(.*)", stem)
    if now_match:
        return tacticTakesHypArgs(now_match.group(1))
    try_match = re.match(r"\s*try\s+(.*)", stem)
    if try_match:
        return tacticTakesHypArgs(try_match.group(1))
    repeat_match = re.match(r"\s*repeat\s+(.*)", stem)
    if repeat_match:
        return tacticTakesHypArgs(repeat_match.group(1))
    return (
        stem == "apply"
        or stem == "eapply"
        or stem == "eexploit"
        or stem == "exploit"
        or stem == "erewrite"
        or stem == "rewrite"
        or stem == "erewrite !"
        or stem == "rewrite !"
        or stem == "erewrite <-"
        or stem == "rewrite <-"
        or stem == "destruct"
        or stem == "elim"
        or stem == "eelim"
        or stem == "inversion"
        or stem == "monadInv"
        or stem == "pattern"
        or stem == "revert"
        or stem == "exact"
        or stem == "eexact"
        or stem == "simpl in"
        or stem == "fold"
        or stem == "generalize"
        or stem == "exists"
        or stem == "case"
        or stem == "inv"
        or stem == "subst"
        or stem == "specialize"
    )


def tacticTakesBinderArgs(stem: str) -> bool:
    return stem == "induction"


def tacticTakesIdentifierArg(stem: str) -> bool:
    return stem == "unfold"


def lemma_name_from_statement(stmt: str) -> str:
    if ("Goal" in stmt or "Obligation" in stmt or re.match(r"\sMorphism\s", stmt)):
        return ""
    stripped_stmt = kill_comments(stmt).strip()

    # Derive match 1
    derive_match = re.fullmatch(
        r"\s*Derive\s+([\w'_]+)\s+SuchThat\s+(.*)\s+As\s+([\w']+)\.\s*",
        stripped_stmt, flags=re.DOTALL)
    if derive_match:
        return derive_match.group(3)

    # Derive match 2
    derive_match = re.fullmatch(
        r"\s*Derive\s+([\w'_]+)\s+(.*)\s+with\s+.*",
        stripped_stmt, flags=re.DOTALL)
    if derive_match:
        return derive_match.group(2)

    # Morphism
    morphism_match = re.fullmatch(
        r"\s*Add(?:\s+Parametric)?\s+Morphism.*\s+as\s+([\w'_]+)\.\s*",
        stripped_stmt, flags=re.DOTALL
    )
    if morphism_match:
        return morphism_match.group(1)

    lemma_match = re.match(
        r"\s*(?:(?:Local|Global)\s+)?(?:" + "|".join(
            normal_lemma_starting_patterns +
            other_starting_patterns +
            assumptions_starting_patterns) +
        r")(?::?\s*|\s+)([\w'\.]*)(.*)",
        stripped_stmt,
        flags=re.DOTALL)
    assert lemma_match, (stripped_stmt, stmt)
    lemma_name = lemma_match.group(1)
    assert ":" not in lemma_name, stripped_stmt
    return lemma_name


symbols_regexp = (r',|(?::>)|(?::(?!=))|(?::=)|\)|\(|;|@\{|~|\+{1,2}|\*{1,2}'
                  r'|&&|\|\||(?<!\\)/(?!\\)|/\\|\\/|(?<![<*+-/|&])=(?!>)|%|'
                  r'(?<!<)-(?!>)|<-|->|<=|>=|<>|\^|\[|\]|(?<!\|)\}|\{(?!\|)')


def get_words(string: str) -> List[str]:
    return [word for word in re.sub(
        r'(\.+|' + symbols_regexp + ')',
        r' \1 ',
        string).split()
            if word.strip() != '']


def get_binder_var(goal: str, binder_idx: int) -> Optional[str]:
    paren_depth = 0
    binders_passed = 0
    skip = False
    forall_match = re.match(r"forall\s+", goal.strip())
    if not forall_match:
        return None
    rest_goal = goal[forall_match.end():]
    for w in get_words(rest_goal):
        if w == "(":
            paren_depth += 1
        elif w == ")":
            paren_depth -= 1
            if paren_depth == 1 or paren_depth == 0:
                skip = False
        elif (paren_depth == 1 or paren_depth == 0) and not skip:
            if w == ":":
                skip = True
            else:
                binders_passed += 1
                if binders_passed == binder_idx:
                    return w
    return None


def normalizeNumericArgs(datum: ScrapedTactic) -> ScrapedTactic:
    numerical_induction_match = re.match(
        r"\s*(induction|destruct)\s+(\d+)\s*\.",
        kill_comments(datum.tactic).strip())
    if numerical_induction_match:
        stem = numerical_induction_match.group(1)
        binder_idx = int(numerical_induction_match.group(2))
        binder_var = get_binder_var(datum.context.fg_goals[0].goal, binder_idx)
        if binder_var:
            newtac = stem + " " + binder_var + "."
            return ScrapedTactic(datum.prev_tactics,
                                 datum.relevant_lemmas,
                                 datum.context, newtac)
        else:
            return datum
    else:
        return datum


def parsePPSubgoal(substr: str) -> Obligation:
    split = re.split("\n====+\n", substr)
    assert len(split) == 2, substr
    hypsstr, goal = split
    return Obligation(parse_hyps(hypsstr), goal)


def summarizeContext(context: ProofContext) -> None:
    eprint("Foreground:")
    for i, subgoal in enumerate(context.fg_goals):
        hyps_str = ",".join(get_first_var_in_hyp(hyp)
                            for hyp in subgoal.hypotheses)
        goal_str = re.sub("\n", "\\n", subgoal.goal)[:100]
        eprint(f"S{i}: {hyps_str} -> {goal_str}")


def isValidCommand(command: str) -> bool:
    command = kill_comments(command)
    goal_selector_match = re.fullmatch(r"\s*\d+\s*:(.*)", command,
                                       flags=re.DOTALL)
    if goal_selector_match:
        return isValidCommand(goal_selector_match.group(1))
    return ((command.strip()[-1] == "."
             and not re.match(r"\s*{", command))
            or re.fullmatch(r"\s*[-+*{}]*\s*", command) is not None) \
        and (command.count('(') == command.count(')'))


def load_commands_preserve(args: argparse.Namespace, file_idx: int,
                           filename: str) -> List[str]:
    try:
        should_show = args.progress
    except AttributeError:
        should_show = False
    try:
        should_show = should_show or args.read_progress
    except AttributeError:
        pass

    try:
        command_limit = args.command_limit
    except AttributeError:
        command_limit = None
    return load_commands(filename, max_commands=command_limit,
                         progress_bar=should_show,
                         progress_bar_offset=file_idx * 2)


def load_commands(filename: str,
                  skip_comments: bool = True,
                  max_commands: Optional[int] = None,
                  progress_bar: bool = False,
                  progress_bar_offset: Optional[int] = None) -> List[str]:
    with open(filename, 'r') as fin:
        contents = fin.read()
    return read_commands(contents,
                         skip_comments=skip_comments,
                         max_commands=max_commands,
                         progress_bar=progress_bar,
                         progress_bar_offset=progress_bar_offset)


def read_commands(contents: str,
                  skip_comments: bool = True,
                  max_commands: Optional[int] = None,
                  progress_bar: bool = False,
                  progress_bar_offset: Optional[int] = None) -> List[str]:
    result: List[str] = []
    cur_command = ""
    comment_depth = 0
    in_quote = False
    curPos = 0

    def search_pat(pat: Pattern) -> Tuple[Optional[Match], int]:
        match = pat.search(contents, curPos)
        return match, match.end() if match else len(contents) + 1

    with tqdm(total=len(contents)+1, file=sys.stdout,
              disable=(not progress_bar),
              position=progress_bar_offset,
              desc="Reading file", leave=False,
              dynamic_ncols=True, bar_format=mybarfmt) as pbar:
        while curPos < len(contents) and (max_commands is None or
                                          len(result) < max_commands):
            _, next_quote = search_pat(re.compile(r"(?<!\\)\""))
            _, next_open_comment = search_pat(re.compile(r"\(\*"))
            _, next_close_comment = search_pat(re.compile(r"\*\)"))
            _, next_bracket = search_pat(re.compile(r"[\{\}]"))
            next_bullet_match, next_bullet = search_pat(
                re.compile(r"[\+\-\*]+(?![\)\+\-\*])"))
            _, next_period = search_pat(
                re.compile(r"(?<!\.)\.($|\s)|\.\.\.($|\s)"))
            nextPos = min(next_quote,
                          next_open_comment, next_close_comment,
                          next_bracket,
                          next_bullet, next_period)
            assert curPos < nextPos
            next_chunk = contents[curPos:nextPos]
            cur_command += next_chunk
            pbar.update(nextPos - curPos)
            if nextPos == next_quote:
                if comment_depth == 0:
                    in_quote = not in_quote
            elif nextPos == next_open_comment:
                if not in_quote:
                    comment_depth += 1
            elif nextPos == next_close_comment:
                if not in_quote and comment_depth > 0:
                    comment_depth -= 1
            elif nextPos == next_bracket:
                if not in_quote and comment_depth == 0 and \
                   re.match(r"\s*(?:\d+\s*:)?\s*$",
                            kill_comments(cur_command[:-1])):
                    result.append(cur_command)
                    cur_command = ""
            elif nextPos == next_bullet:
                assert next_bullet_match
                match_length = next_bullet_match.end() - \
                    next_bullet_match.start()
                if not in_quote and comment_depth == 0 and \
                   re.match(r"\s*$",
                            kill_comments(cur_command[:-match_length])):
                    result.append(cur_command)
                    cur_command = ""
                assert next_bullet_match.end() >= nextPos
            elif nextPos == next_period:
                if not in_quote and comment_depth == 0:
                    result.append(cur_command)
                    cur_command = ""
            curPos = nextPos

    if skip_comments:
        result = [kill_comments(cmd).strip() for cmd in result]
    return result


parsePat = re.compile("[() ]", flags=(re.ASCII | re.IGNORECASE))


def searchStrsInMsg(sexp, fuel: int = 30) -> List[str]:
    if isinstance(sexp, list) and len(sexp) > 0 and fuel > 0:
        if sexp[0] == "str" or sexp[0] == Symbol("str"):
            assert len(sexp) == 2 and isinstance(sexp[1], str)
            return [sexp[1]]
        else:
            return [substr
                    for substrs in [searchStrsInMsg(sublst, fuel - 1)
                                    for sublst in sexp]
                    for substr in substrs]
    return []


def get_module_from_filename(filename: Union[Path, str]) -> str:
    return Path(filename).stem


def symbol_matches(full_symbol: str, shorthand_symbol: str) -> bool:
    if full_symbol == shorthand_symbol:
        return True
    else:
        return full_symbol.split(".")[-1] == shorthand_symbol
    pass


def subgoalSurjective(newsub: Obligation, oldsub: Obligation) -> bool:
    oldhyp_terms = [get_hyp_type(hyp) for hyp in oldsub.hypotheses]
    for newhyp_term in [get_hyp_type(hyp) for hyp in newsub.hypotheses]:
        if newhyp_term not in oldhyp_terms:
            return False
    return newsub.goal == oldsub.goal


def contextSurjective(newcontext: ProofContext, oldcontext: ProofContext):
    for oldsub in oldcontext.all_goals:
        if not any([subgoalSurjective(newsub, oldsub)
                    for newsub in newcontext.all_goals]):
            return False
    return len(newcontext.all_goals) >= len(oldcontext.all_goals)


def lemmas_in_file(filename: str, cmds: List[str],
                   include_proof_relevant: bool = False) \
        -> List[Tuple[str, str]]:
    lemmas = []
    proof_relevant = False
    in_proof = False
    for cmd_idx, cmd in reversed(list(enumerate(cmds))):
        if in_proof and possibly_starting_proof(cmd):
            in_proof = False
            proof_relevant = proof_relevant or \
                cmd.strip().startswith("Derive") or \
                cmd.strip().startswith("Equations")
            if not proof_relevant or include_proof_relevant:
                lemmas.append((cmd_idx, cmd))
        if ending_proof(cmd):
            in_proof = True
            proof_relevant = cmd.strip() == "Defined."
    sm_stack = initial_sm_stack(filename)
    full_lemmas = []
    obl_num = 0
    last_program_statement = ""
    for cmd_idx, cmd in enumerate(cmds):
        scmd = kill_comments(cmd).strip()
        sm_stack = update_sm_stack(sm_stack, cmd)
        if (cmd_idx, cmd) in lemmas:
            if re.match(r"\s*Next\s+Obligation\s*\.\s*",
                        scmd):
                assert last_program_statement != ""
                unique_lemma_statement = f"{last_program_statement} Obligation {obl_num}."
                obl_num += 1
            else:
                unique_lemma_statement = cmd
            full_lemmas.append((sm_prefix_from_stack(
                sm_stack), unique_lemma_statement))
        if re.match(r"\s*Program\s+.*", scmd):
            last_program_statement = cmd
            obl_num = 0
    return full_lemmas


def let_to_hyp(let_cmd: str) -> str:
    let_match = re.match(r"\s*Let(?:\s+Fixpoint)?\s+(.*)\.\s*$",
                         let_cmd,
                         flags=re.DOTALL)
    assert let_match, "Command passed in isn't a Let!"
    split = split_by_char_outside_matching(r"\(", r"\)", ":=",
                                           let_match.group(1))
    if split:
        name_and_type, body = split
    else:
        name_and_type = let_match.group(1)

    name_and_prebinders, ty = \
        unwrap(split_by_char_outside_matching(r"\(", r"\)", ":",
                                              name_and_type))
    prebinders_match = re.match(
        r"\s*([\w']*)([^{}]*)",
        name_and_prebinders)
    assert prebinders_match, \
        f"{name_and_prebinders} doesn't match prebinders pattern"
    name = prebinders_match.group(1)
    prebinders = prebinders_match.group(2)
    if prebinders.strip() != "":
        prebinders = f"forall {prebinders},"

    return f"{name} : {prebinders} {ty[1:]}."


def admit_proof_cmds(lemma_statement: str) -> List[str]:
    let_match = re.match(r"\s*Let(?:\s+Fixpoint)?\s+(.*)\.$",
                         lemma_statement,
                         flags=re.DOTALL)
    if let_match and ":=" not in lemma_statement:
        admitted_defn = f"Hypothesis {let_to_hyp(lemma_statement)}"
        return ["Abort.", admitted_defn]
    return ["Admitted."]


def admit_proof(coq: SerapiInstance,
                lemma_statement: str) -> List[str]:
    admit_cmds = admit_proof_cmds(lemma_statement)
    for cmd in admit_cmds:
        coq.run_stmt(cmd)
    return admit_cmds


def _is_parentheses_correct(line):

    # 1. check for parentheses structure
    square = 0
    round = 0
    for char in line:
        if char == '(':
            round += 1
        elif char == ')':
            round -= 1
        elif char == '[':
            square += 1
        elif char == ']':
            square -= 1

        if round < 0 or square < 0:
            return False
    return square == 0 and round == 0


def _is_match_goal_correct(line):
    # TODO: make this function to find nested "match goal" structures

    # 2. check `match goal with ... end` structure
    match = re.match('.*match\s+goal\s+with', line, re.DOTALL)
    if match is not None:
        match = re.match('.*match\s+goal\s+with(.*)end', line, re.DOTALL)
        if match is None:
            return False
    return True


def _replace_bullet_tactic(tactic):
    if tactic == 'left':
        return 'constructor 1'
    elif tactic == 'right':
        return 'constructor 2'
    return tactic


def _split_square_brackets(tactic):
    # check whether tactic contains nested `[ ... ]` structure
    # in this case return tactic as is.
    tactic_split = re.split(r"\|(?!.*[\]])", tactic[1:-2])
    tactic_split = [t.strip() + '.' if t else 'idtac.' for t in tactic_split]

    return tactic_split


def split_goal_idx_tactic(tactic_str):
    """
    Split the tactic string:
        if the string startswith <n>: <tactic>, then the function returns [<n>, <tactic>],
        else it returns [tactic_str]
    Args:
        tactic_str: str

    Returns:
        list[str]
    """
    goal_idx_match = re.match(r"all\s*:", tactic_str)
    if goal_idx_match:
        return 'all', re.split(r"all\s*:\s*", tactic_str)[-1]

    goal_idx_match = re.match(r"(\d+)\s*:", tactic_str)
    if not goal_idx_match:
        return [None, tactic_str]
    else:
        goal_idx = int(goal_idx_match.group(1)) - 1
        return goal_idx, re.split(r"\d+\s*:\s*", tactic_str, maxsplit=1)[-1]


def linearize_commands(project_path, module_path, remove_bullets=False, timeout=10):
    commands = load_commands(module_path, skip_comments=True)

    coq = SerapiInstance(['sertop', '--implicit', '--omit_loc'], module_path,
                         project_path, timeout=timeout)

    in_proof = False
    linear_commands = []
    prev_cmd = ''
    for cmd in commands:
        # Check parenthesis structure.
        if prev_cmd:
            cmd = prev_cmd + cmd
            prev_cmd = ''

        if not _is_parentheses_correct(cmd):
            prev_cmd = cmd
            continue

        if in_proof:
            cmd = cmd.strip()

            # skip bullet
            if (cmd[-1] in ['+', '-', '*', '{', '}'] or
                'Ltac' in cmd or
                'cycle ' in cmd or
                cmd.endswith('...') or
                cmd.startswith('Proof') or
                'try now rewrite get_set_diff in *;' in cmd  # this is ugly, but I don't know how to parse this correctly
            ):
                coq.run_stmt(cmd)
                linear_commands.append(cmd)
            elif cmd:

                # split by semicolon but not inside parentheses
                terms = cmd.split(';')
                terms[-1] = terms[-1][:-1] if terms[-1][-1] == '.' else terms[-1]  # remove dot from the last term
                i = 0
                while i < len(terms):
                    terms[i] = terms[i].strip()
                    if not _is_parentheses_correct(terms[i]) or not _is_match_goal_correct(terms[i]):
                        terms[i] = f"{terms[i]}; {terms[i + 1].strip()}"
                        del terms[i + 1]
                    else:
                        i += 1

                # run first tactic
                n_goals = len(coq.proof_context.fg_goals)

                first_tac = ''
                checkpoint_state = coq._hist[-1][-1]

                for i, term in enumerate(terms.copy()):
                    next_tac = _replace_bullet_tactic(term) + '.'
                    first_tac = next_tac if not first_tac else f"{first_tac[:-1]}; {next_tac}"
                    first_tac_goal_idx, _ = split_goal_idx_tactic(first_tac)

                    try:
                        coq.run_stmt(first_tac)
                        break
                    except SerapiException as e:
                        terms = terms[1:]

                tactics_to_append = [first_tac]

                n_tactics_to_apply = 0
                if coq.proof_context is not None:
                    if isinstance(first_tac_goal_idx, str) and first_tac_goal_idx == 'all':
                        n_tactics_to_apply = len(coq.proof_context.fg_goals)
                    else:
                        n_tactics_to_apply = len(coq.proof_context.fg_goals) - n_goals + 1

                # run other tactic taking into account ";" and "[ ... | ... ]" syntax
                prev_tactic = ''
                for i, tactic in enumerate(terms[1:]):
                    tactic = _replace_bullet_tactic(tactic) + '.'
                    tactic = f"{prev_tactic[:-1]}; {tactic}" if prev_tactic else tactic

                    if first_tac_goal_idx is not None:
                        goal_idx = first_tac_goal_idx
                    else:
                        goal_idx, tactic = split_goal_idx_tactic(tactic)

                    if isinstance(goal_idx, int):
                        goal_idx += 1
                    elif goal_idx is None or goal_idx == 'all':
                        goal_idx = 1

                    goal_idx_backup = goal_idx

                    if not tactic.startswith('['):
                        tactics_to_apply = [tactic] * n_tactics_to_apply
                    else:
                        tactics_to_apply = _split_square_brackets(tactic)
                        if len(tactics_to_apply) == 1:
                            goal_idx = 'all'

                    try:
                        current_tactics = []
                        for tac in tactics_to_apply:
                            n_goals_before = len(coq.proof_context.fg_goals)

                            coq.run_stmt(f"{goal_idx}: {tac}")
                            current_tactics.append(f"{goal_idx}: {tac}")

                            n_goals_after = len(coq.proof_context.fg_goals)
                            n_new_goals = n_goals_after - n_goals_before

                            # 3 possibilities:
                            # 1. number of goals didn't change -> goal_idx += 1
                            # 2. Number of goals decreased by 1 -> pass
                            # 3. Number of goals increased by k -> goals_idx += k - 1
                            if isinstance(goal_idx, str) and goal_idx == 'all':
                                goal_idx = goal_idx_backup
                            if n_new_goals == 0:
                                goal_idx += 1
                            elif n_new_goals > 0:
                                goal_idx += n_new_goals + 1

                        n_tactics_to_apply = 0
                        if isinstance(first_tac_goal_idx, str) and first_tac_goal_idx == 'all':
                            n_tactics_to_apply = len(coq.proof_context.fg_goals)
                        elif coq.proof_context is not None:
                            n_tactics_to_apply = len(coq.proof_context.fg_goals) - n_goals + 1

                        prev_tactic = ''
                        tactics_to_append.extend(current_tactics)
                    except SerapiException as e:
                        prev_tactic = tactic

                        # TODO: something went wrong
                        if coq.cur_state < checkpoint_state:
                            raise e

                # if application of the last terms is unsuccessfull just accept cmd as is
                if prev_tactic:
                    while coq.cur_state != checkpoint_state:
                        coq.cancel_last()
                    coq.run_stmt(cmd)
                    linear_commands.append(cmd)
                else:
                    linear_commands.extend(tactics_to_append)
        else:
            coq.run_stmt(cmd)
            linear_commands.append(cmd)

        if not in_proof and coq.proof_context:
            in_proof = True
        elif coq.proof_context is None:
            in_proof = False

    coq.kill()
    return linear_commands


def print_mod_path(modpath):
    if str(modpath[0]) == "MPdot":
        return print_mod_path(modpath[1]) + "." + str(modpath[2][1])
    elif str(modpath[0]) == "MPfile":
        return ".".join([str(x[1]) for x in modpath[1][1]][::-1])
    else:
        assert str(modpath[0]) == "MPbound"
        return ".".join(
            [str(x[1]) for x in modpath[1][2][1]][::-1]
            + [str(modpath[1][1][1])]
        )


def mod_path_file(modpath):
    if str(modpath[0]) == "MPdot":
        return mod_path_file(modpath[1])
    elif str(modpath[0]) == "MPfile":
        return ".".join([str(x[1]) for x in modpath[1][1]][::-1])
    else:
        assert str(modpath[0]) == "MPbound"
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Module for interacting with a coq-serapi instance "
        "from Python (3).")
    parser.add_argument(
        "--prelude", default=".", type=str,
        help="The `home` directory in which to look for the _CoqProject file.")
    parser.add_argument(
        "--includes", default=None, type=str,
        help="The include options to pass to coq, as a single string. "
        "If none are provided, we'll attempt to read a _CoqProject "
        "located in the prelude directory, and fall back to no arguments "
        "if none exists.")
    parser.add_argument(
        "--sertop", default="sertop",
        dest="sertopbin", type=str,
        help="The location of the serapi (sertop) binary to use.")
    parser.add_argument(
        "--srcfile", "-f", nargs='*', dest='srcfiles', default=[], type=str,
        help="Coq source file(s) to execute.")
    parser.add_argument(
        "--interactive", "-i",
        action='store_const', const=True, default=False,
        help="Drop into a pdb prompt after executing source file(s). "
        "A `coq` object will be in scope as an instance of SerapiInstance, "
        "and will kill the process when you leave.")
    parser.add_argument("--verbose", "-v",
                        action='store_const', const=True, default=False)
    parser.add_argument("--progress",
                        action='store_const', const=True, default=False)
    args = parser.parse_args()
    includes = ""
    if args.includes:
        includes = args.includes
    else:
        with contextlib.suppress(FileNotFoundError):
            with open(f"{args.prelude}/_CoqProject", 'r') as includesfile:
                includes = includesfile.read()
    with SerapiContext([args.sertopbin],
                       "",
                       includes, args.prelude) as coq:
        def handle_interrupt(*args):
            nonlocal coq
            print("Running coq interrupt")
            coq.interrupt()

        with sighandler_context(signal.SIGINT, handle_interrupt):
            for srcpath in args.srcfiles:
                commands = load_commands(srcpath)
                for cmd in commands:
                    eprint(f"Running: \"{cmd}\"")
                    coq.run_stmt(cmd)
            if args.interactive:
                breakpoint()
                x = 50


if __name__ == "__main__":
    main()
