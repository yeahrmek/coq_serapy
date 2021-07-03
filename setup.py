import setuptools

setuptools.setup(
    name="coq_serapy",
    version="0.1",
    author="",
    description="Python interface for interacting with coq-serapi",
    packages=["coq_serapy"],
    python_requires=">=3.6",
    requires=["pampy", "sexpdata", "tqdm"]
)
