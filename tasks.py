import datetime
import enum
import glob
import os
import random
import sys
import typing
import warnings
from pathlib import Path

# end stdlib, start try:

try:
    import edwh
    import edwh.tasks  # stupide pycharm!
    import humanize
    from edwh import DOCKER_COMPOSE
    from edwh import improved_task as task
    from edwh.__about__ import __version__ as EDWH_VERSION
    from invoke import Context, Result
    from termcolor import colored, cprint
    from typing_extensions import Self

except ImportError as import_err:
    if sys.argv[0].split("/")[-1] in ("inv", "invoke"):
        print("WARNING: this tasks.py works best using the edwh command instead of using inv[oke] directly.")
        print("Example:")
        if sys.argv[1].startswith("-"):
            print("> edwh", " ".join(sys.argv[1:]))
        else:
            print("> edwh local." + " ".join(sys.argv[1:]))
        print()

    print("Install edwh using `pipx install edwh[omgeving]` to automatically install edwh and all dependencies.")
    print("Or install using `pip install -r requirements.txt` in an appropriate virtualenv when not using edwh. ")
    print()
    print("ImportError:", import_err)

    exit(1)

MINIMAL_REQUIRED_EDWH_VERSION = "0.53.0"

if EDWH_VERSION < MINIMAL_REQUIRED_EDWH_VERSION:
    # todo: natural sort?
    cprint(
        f"Note: your `edwh` tool might be outdated ({EDWH_VERSION} < {MINIMAL_REQUIRED_EDWH_VERSION}). "
        "If this leads to weird behavior, try running `edwh self-update`",
        color="yellow",
    )


def find_differences_in_dictionaries(what_is: dict, what_should_be: dict, prefix: str) -> bool:
    """Find differences between dicts, useful for testing template keys in a given larger config file.
    Example:
        find_differences_in_dictionaries(
            config["pytest"], template["pytest"], "pytest/"
        )
    """
    found_missing = False
    for key, value in what_should_be.items():
        if key not in what_is:
            found_missing = True
            print(f"Missing: {prefix}/{key}: {value}")
        if isinstance(value, dict):
            # recursively find differences for each dict
            found_missing = found_missing or find_differences_in_dictionaries(
                what_is.get(key, {}),
                what_should_be[key],
                prefix=os.path.join(prefix, key),
            )
    return found_missing


class SomethingWentWrong(Exception):
    pass


def failsafe(c: typing.Callable[[], Result]) -> None:
    """Executes the callable, and if not result.ok raises a RemoteWentWrong exception."""
    result = c()
    if not result.ok:
        raise SomethingWentWrong(result.stderr)


class WiseException(EnvironmentError):
    pass


class classproperty(property):
    # combining classmethod + property was removed in 3.13 for some reason
    # https://stackoverflow.com/questions/128573/using-property-on-classmethods
    def __get__(self, owner_self, owner_cls):
        return self.fget(owner_cls)


class StateOfDevelopment(enum.Enum):
    ONT = "Ontwikkel"
    DEMO = "Demo"
    TEST = "Test"
    UAT = "User Acceptance Testing"
    PRD = "Productie"

    @classmethod
    def from_env(cls) -> Self:
        return cls[edwh.get_env_value("STATE_OF_DEVELOPMENT")]

    @classmethod
    def from_key(cls, key: str) -> Self:
        # same as StateOfDevelopment[key]
        return cls[key]

    @classmethod
    def from_value(cls, value: str) -> Self:
        # same as StateOfDevelopment(value)
        return cls(value)

    @classproperty
    def options(cls) -> list[str]:
        return cls._member_names_

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"StateOfDevelopment({self.value})"

    def is_productie(self) -> bool:
        return self.name in ("DEMO", "UAT", "PRD")

    def is_ontwikkel(self) -> bool:
        return self.name in ("ONT", "TEST")


def productie_prompt(prompt: str):
    sod = StateOfDevelopment.from_env()
    if sod.is_ontwikkel():
        return

    print(f"Dit lijkt op een productie machine (STATE_OF_DEVELOPMMENT={sod})")
    choice = input(f"{prompt} [ja,NEE]")
    if choice != "ja":
        print("Verstandig!")

        raise WiseException("Productie database - niet aankomen")

DEFAULT_UNSAFE_PASSWORD = "test"

@task()
def setup(c: Context):
    """Setup or update the ontwikkel_omgeving environment."""
    # this is the platform-specific setup hook, which is executed after running the global logic of `ew setup`.
    # normally, you would not run `ew local.setup`.
    print("Setting up/updating  ontwikkel_omgeving ...")
    dotenv_path = Path(".env")
    if not dotenv_path.exists():
        dotenv_path.touch()

    # check these options
    hosting_domain = edwh.check_env(
        key="HOSTINGDOMAIN",
        default="localhost",
        comment="hostname like meteddie.nl; edwh.nl; localhost; robin.edwh.nl; dockers.local",
    )

    stripped_domain = hosting_domain.strip().replace(".", "_").upper()

    hosting_name = edwh.check_env(
        key="APPLICATION_NAME",
        default="hetnieuwedelen",
        comment="used for routing traefik. [www.]<applicationname>.<hostingdomain>, "
        "alongside py4web which is also registered.",
    )
    edwh.check_env(
        key="CERTRESOLVER",
        default="default",
        comment="which certresolver to use - default|staging|letsencrypt. See reverse proxy setup for options",
        allowed_values=("default", "staging", "letsencrypt"),
    )
    _state_of_development = edwh.check_env(
        key="STATE_OF_DEVELOPMENT",
        default="",
        comment="ontwikkel: ONT|test: TEST|demo: DEMO |user acceptance testing: UAT|productie: PRD",
        allowed_values=StateOfDevelopment.options,
    )

    state_of_development = StateOfDevelopment.from_key(_state_of_development)

    internet_accessible = (
        edwh.check_env(
            key="INTERNET_ACCESSIBLE",
            default="0",
            comment="If local you may use default (unsafe) passwords, but not if it is publicly accessible",
            allowed_values=("0", "1"),
        )
        == "1"
    )

    if state_of_development.is_ontwikkel():
        use_default = (
            edwh.check_env(
                key="ACCEPT_DEFAULTS",
                default="1",
                comment="Do you want to accept default (development) values for the rest of the env variables?",
                allowed_values=("1", "0"),
            )
            == "1"
        )
    else:
        use_default = False



@task()
def pip_bump_all(c: Context):
    """
    Bump all .txt files using `edwh pip.upgrade`.

    All used is:
        - web2py
        - py4web
        - migrate
        - jupyterlab
    """
    c.run("edwh pip.upgrade web2py,py4web,jupyterlab,migrate", pty=True)


@task()
def migrate(ctx):
    if not hasattr(edwh.tasks, "migrate"):
        raise EnvironmentError("Your `edwh` is not up-to-date! Please self-update and run `edwh migrate`")

    cprint(
        "local.migrate is deprecated. Please use `edwh migrate` instead.",
        color="yellow",
    )
    return edwh.tasks.migrate(ctx)


