from typing import Tuple, Optional, Union
from pathlib import Path

from gym import Env

from . import SerapiInstance, SerapiException


class CoqEnv(SerapiInstance, Env):
    """
    Coq environment for proving given theorem.

    Args:
    -----
        coq_projects_path : path-like,
            Path to the lean-gym directory

        coq_commands : list[str]
            Coq commands required to be executed before the theorem statement
            we would like to prove.
            The last element in the list is the theorem statement itself.

        timeout : int, default=120
            Timeout for lean commands execution
    """
    def __init__(self, coq_projects_path: str,
                 coq_commands: list[str],
                 timeout: Optional[float] = None) -> None:
        self.coq_projects_path = Path(coq_projects_path)
        self.coq_commands = coq_commands

        super().__init__(['sertop', '--implicit', '--omit_loc'],
                         self.coq_projects_path / 'dummy.v',
                         self.coq_projects_path,
                         timeout)

        self.proof_search_id = None

    def step(self, action: str) -> dict:
        """
        Run given tactic for a given search at given state

        Args:
        -----
            action : str
                Tactic to be applied

        Returns:
        --------
            observation : ProofContext
                New state id and its string represenation (new goals).
                If tactic application fails it returns `(-1, '')`

            reward : float
                0 - for incorrect tactic or if proof is not complete
                1 - if no goals returned

            done : bool
                End of proof flag

            info : dict
                Dict with error message if it occured
        """
        info = {'error': None}
        try:
            self.run_stmt(action)
        except SerapiException as exc:
            info['error'] = str(exc)
            info['tactic_state'] = None
            info['tactic_state_id'] = None

        observation, reward, done = None, 0.0, False
        if info['error'] is None:
            proof_context = self.proof_context
            if proof_context is None or not proof_context.all_goals:
                proof_context = "no goals"
                reward = 1.0
                done = True

            info['tactic_state_id'] = self.cur_state
            info['tactic_state'] = proof_context
            observation = proof_context

        return observation, reward, done, info

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        return_info: bool = False,
        options: Optional[dict] = None,
    ) -> Union[str, Tuple[str, dict]]:
        """
        Returns initial observation

        Args:
        ----
            seed : Optional[int], default=None
                The seed that is used to initialize the environment.
                Does not take effect in current evnironment

            return_info : bool, default=False
                If `True` --- additional dictionary with info will be returned

            options : Optional[dict], default=None
                Additional options to initialize environment with.
                Does not take effect currently.
                In future it can be used to initialize env with different theorems.
        """
        if self.proof_search_id is None:
            for cmd in self.coq_commands:
                self.run_stmt(cmd)
            self.proof_search_id = self.cur_state
        else:
            while self.cur_state > self.proof_search_id:
                self.cancel_last()

            if self.cur_state != self.proof_search_id:
                self.kill()
                self.init()
                self.proof_search_id = None
                return self.reset()

        observation = self.proof_context

        if return_info:
            return observation, {}

        return observation

    def close(self):
        """
        Perform any necessary cleanup
        """
        self.kill()
