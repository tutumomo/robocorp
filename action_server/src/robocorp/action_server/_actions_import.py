import json
import logging
import subprocess
import sys
import typing
from pathlib import Path
from typing import Optional

from termcolor import colored

if typing.TYPE_CHECKING:
    from robocorp.action_server._models import ActionPackage

log = logging.getLogger(__name__)


class ActionPackageError(Exception):
    pass


def import_action_package(
    *,
    datadir: Path,
    action_package_dir: str,
    name: Optional[str],
    disable_not_imported: bool,
    skip_lint: bool,
):
    """
    Imports action packages based on directories given in the filesystem.

    Note that the action package is expected to be in the proper directory at
    this point (meaning that it should have been extracted under the /datadir
    if given as a .zip or a path in the filesystem in any other place when
    running in dev mode).

    Raises:
        ActionPackageError if it was not recognized as an action package.

    Note:
        This can be a slow operation as it may require building the RCC
        environment.
    """

    from ._errors_action_server import ActionServerValidationError
    from ._gen_ids import gen_uuid
    from ._models import ActionPackage
    from ._rcc import create_hash, get_rcc
    from ._robo_utils.process import build_python_launch_env
    from ._settings import is_frozen

    log.debug("Importing action package from: %s", action_package_dir)

    datadir = datadir.absolute()
    import_path = Path(action_package_dir).absolute()
    if not import_path.exists():
        raise ActionPackageError(
            f"Unable to import action package from directory: {import_path} "
            "(directory does not exist).",
        )
    if not import_path.is_dir():
        raise ActionPackageError(f"Error: expected {import_path} to be a directory.")

    import yaml

    # Verify if it's actually a proper package (meaning that it has
    # the action-server.yaml as well as actions we can run).
    original_action_server_yaml = action_server_yaml = (
        import_path / "action-server.yaml"
    )
    action_server_yaml_exists = action_server_yaml.exists()
    if not action_server_yaml_exists:
        # Backward-compatibility
        action_server_yaml = import_path / "conda.yaml"
        action_server_yaml_exists = action_server_yaml.exists()
        if action_server_yaml_exists:
            log.critical(
                "Deprecation message: The file for defining the environment is now `action-server.yaml`.\n"
                f"Please rename `conda.yaml` to `action-server.yaml` at {action_server_yaml}"
            )

    if not action_server_yaml_exists:
        if is_frozen():
            raise ActionServerValidationError(
                f"Unable to import actions in standalone action-server because no `action-server.yaml` is available at: {original_action_server_yaml}."
            )
        log.info(
            """Adding action without a managed environment (action-server.yaml unavailable).
Note: no virtual environment will be used for the imported actions, they'll be run in the same environment used to run the action server."""
        )
        condahash = "<unmanaged>"
        use_env = {}
    else:
        try:
            with open(action_server_yaml, "r", encoding="utf-8") as stream:
                contents = yaml.safe_load(stream)
        except Exception:
            raise ActionPackageError(
                f"{action_server_yaml} does not seem a valid yaml."
            )

        if not isinstance(contents, dict):
            raise ActionPackageError(f"{action_server_yaml} has no dict as top-level.")

        if not contents.get("dependencies"):
            raise ActionPackageError(
                f"{action_server_yaml} has no 'dependencies' specified."
            )

        log.debug(
            f"""Actions added with managed environment defined by: {action_server_yaml}."""
        )

        # The hash is based only on the parsed contents, not on the file
        # contents per se (so, changing comments or spaces is ok).
        condahash = create_hash(repr(contents))

        log.info(
            "Action package seems ok. "
            "Bootstrapping RCC environment (please wait, this can take a long time)."
        )
        rcc = get_rcc()
        env_info = rcc.create_env_and_get_vars(action_server_yaml, condahash)
        if not env_info.success:
            raise ActionPackageError(
                f"It was not possible to bootstrap the RCC environment. "
                f"Error: {env_info.message}"
            )
        if not env_info.result:
            raise ActionPackageError(
                "It was not possible to get the environment when "
                "bootstrapping RCC environment."
            )
        use_env = env_info.result.env

    # Ok, we bootstrapped, now, let's collect the actions.
    try:
        # If the directory can be made relative to the datadir, save the
        # directory as relative.
        directory_path = import_path.relative_to(datadir)
        assert import_path.samefile(directory_path)
    except (AssertionError, ValueError):
        # Otherwise use the absolute path.
        directory_path = import_path

    action_package_id = gen_uuid("action_package")
    if not name:
        name = import_path.name

    python_exe = use_env.get("PYTHON_EXE")
    if python_exe:
        log.info(colored(f"Python interpreter path: {python_exe}", attrs=["dark"]))

    action_package = ActionPackage(
        id=action_package_id,
        name=name,
        directory=directory_path.as_posix(),
        conda_hash=condahash,
        env_json=json.dumps(use_env),
    )
    log.debug(f"Collecting actions for Action Package: {name}.")

    env = build_python_launch_env(use_env)

    v = _get_robocorp_actions_version(env, import_path)
    expected_version = (0, 0, 7)
    expected_version_str = ".".join(str(x) for x in expected_version)
    if v < expected_version:
        v_as_str = ".".join(str(x) for x in v)

        if action_server_yaml_exists:
            raise ActionServerValidationError(
                f"Error, the `robocorp-actions` version is: {v_as_str}.\n"
                f"Expected `robocorp-actions` version to be {expected_version_str} or higher.\n"
                f"Please update the version in: {action_server_yaml}\n"
            )
        else:
            raise ActionServerValidationError(
                f"Error, the `robocorp-actions` version is: {v_as_str}.\n"
                f"Expected it to be {expected_version_str} or higher.\n"
                f"Please update the `robocorp-actions` version in your python environment (python: {sys.executable})\n"
            )

    _add_actions_to_db(
        datadir,
        env,
        import_path,
        action_package,
        disable_not_imported=disable_not_imported,
        skip_lint=skip_lint,
    )


def _get_robocorp_actions_version(env, cwd) -> tuple[int, ...]:
    from robocorp.action_server._settings import get_python_exe_from_env

    python = get_python_exe_from_env(env)
    cmdline: list[str] = [
        python,
        "-c",
        "import robocorp.actions;print(robocorp.actions.__version__)",
    ]
    msg = f"""Unable to get robocorp.actions version.

This usually means that `robocorp.actions` is not installed in the python
environment (if `action-server.yaml` is present, make sure that `robocorp-actions`
is defined in the environment, otherwise make sure that `robocorp-actions`
is installed in the same environment being used to run the action server).

Python executable being used:
{python}
"""

    try:
        output = subprocess.check_output(
            cmdline,
            env=env,
            cwd=cwd,
        )
    except Exception:
        raise RuntimeError(msg)
    str_output = output.decode("utf-8", "replace")
    try:
        return tuple(int(x) for x in str_output.strip().split("."))
    except Exception:
        raise RuntimeError(msg)


def _add_actions_to_db(
    datadir: Path,
    env: dict,
    import_path: Path,
    action_package: "ActionPackage",
    disable_not_imported: bool,
    skip_lint: bool,
):
    from dataclasses import asdict

    from robocorp.actions._lint_action import format_lint_results

    from robocorp.action_server._errors_action_server import ActionServerValidationError
    from robocorp.action_server._gen_ids import gen_uuid
    from robocorp.action_server._models import Action, ActionPackage, get_db
    from robocorp.action_server._settings import get_python_exe_from_env

    python = get_python_exe_from_env(env)

    cmdline = [python, "-m", "robocorp.actions", "list"]

    if skip_lint:
        cmdline.append("--skip-lint")

    popen = subprocess.Popen(
        cmdline,
        env=env,
        cwd=str(import_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    stdout, stderr = popen.communicate()
    if popen.poll() != 0:
        if popen.poll() == 1:
            # Let's see if we have linting issues.
            try:
                loaded_from_stdout = json.loads(stdout)
            except Exception:
                pass  # Ok, just go with the "regular" error.
            else:
                # We loaded some json from the stdout. Check if there
                # were linting errors.
                if isinstance(loaded_from_stdout, dict):
                    lint_result = loaded_from_stdout.get("lint_result")
                    if isinstance(lint_result, dict):
                        formatted_lint_result = format_lint_results(lint_result)
                        if formatted_lint_result is not None:
                            raise ActionServerValidationError(
                                formatted_lint_result.message
                            )

        raise RuntimeError(
            f"It was not possible to list the actions.\n"
            f"cmdline: {subprocess.list2cmdline(cmdline)}\n"
            f"cwd: {import_path}\n"
            f"stdout:{stdout.decode('utf-8', 'replace')}\n"
            f"stderr:{stderr.decode('utf-8', 'replace')}"
        )

    # If it didn't fail the import, consider as warning (and thus print in yellow).
    decoded_stderr = stderr.decode("utf-8", "replace").strip()
    if decoded_stderr:
        log.critical(colored(f"{decoded_stderr}\n", color="yellow", attrs=["bold"]))

    try:
        loaded = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"It was not possible to load as json the contents >>{stdout!r}<<"
        )
    else:
        actions = []
        for action_fields in loaded:
            filepath = Path(action_fields["file"]).absolute()
            try:
                filepath = filepath.relative_to(import_path)
            except ValueError:
                pass

            actions.append(
                Action(
                    id=gen_uuid("action"),
                    action_package_id=action_package.id,
                    name=action_fields["name"],
                    docs=action_fields["docs"],
                    file=filepath.as_posix(),
                    lineno=action_fields["line"],
                    input_schema=json.dumps(action_fields["input_schema"]),
                    output_schema=json.dumps(action_fields["output_schema"]),
                    enabled=True,
                    is_consequential=(action_fields.get("options") or {}).get(
                        "is_consequential", None
                    ),
                )
            )

    db = get_db()
    try:
        existing_action_package = db.first(
            ActionPackage,
            "SELECT * FROM action_package WHERE name = ?",
            [action_package.name],
        )
    except KeyError:
        # ok, insert as new one
        with db.transaction():
            log.info("Found new action package: %s", action_package.name)
            db.insert(action_package)
            for action in actions:
                log.info("Found new action: %s", action.name)
                db.insert(action)
    else:
        # We already have an existing action package with the same name. This
        # means we'll have to update it instead of adding a new one.
        new_action_package_as_dict = asdict(action_package)
        # The id should not be updated.
        del new_action_package_as_dict["id"]

        existing_actions = db.select(
            Action,
            "SELECT * FROM action WHERE action_package_id = ?",
            [existing_action_package.id],
        )

        existing_action_name_to_action = {}
        for action in existing_actions:
            existing_action_name_to_action[action.name] = action

        if disable_not_imported:
            all_actions = db.all(Action)

        seen_action_ids = set()
        with db.transaction():
            log.debug("Updating action package: %s", action_package.name)
            db.update_by_id(
                ActionPackage,
                existing_action_package.id,
                new_action_package_as_dict,
            )
            if not actions:
                log.info("Found no actions in: %s", action_package.name)

            for action in actions:
                action.action_package_id = existing_action_package.id
                existing_action = existing_action_name_to_action.get(action.name)
                if existing_action is not None:
                    # This is an existing action, we need to update it.
                    new_action_as_dict = asdict(action)
                    del new_action_as_dict["id"]
                    log.debug("Updating action: %s", action.name)
                    db.update_by_id(Action, existing_action.id, new_action_as_dict)
                    seen_action_ids.add(existing_action.id)
                else:
                    # This is a new action, insert it.
                    log.info("Found new action: %s", action.name)
                    db.insert(action)
                    seen_action_ids.add(action.id)

            if disable_not_imported:
                for action in all_actions:
                    if action.id not in seen_action_ids:
                        log.info("Disabling action: %s", action.name)
                        db.update_by_id(Action, action.id, dict(enabled=False))
