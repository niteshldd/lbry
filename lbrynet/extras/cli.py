import sys
import os
import json
import asyncio
import argparse
import typing
import logging.handlers
from aiohttp.client_exceptions import ClientConnectorError
from docopt import docopt
from textwrap import dedent

from lbrynet import conf
from lbrynet.utils import json_dumps_pretty
from lbrynet.extras.daemon.Daemon import Daemon
from lbrynet.extras.daemon.client import LBRYAPIClient, JSONRPCException
from lbrynet.extras.system_info import get_platform
from lbrynet.extras.daemon.loggly_handler import get_loggly_handler

log = logging.getLogger("lbrynet")
default_formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s:%(lineno)d: %(message)s")

optional_path_getter_type = typing.Optional[typing.Callable[[], str]]


def setup_logging(console: bool, verbose: bool, use_loggly: bool):
    file_handler = logging.handlers.RotatingFileHandler(conf.settings.get_log_filename(),
                                                        maxBytes=2097152, backupCount=5)
    file_handler.setFormatter(default_formatter)
    log.addHandler(file_handler)

    if console:
        handler = logging.StreamHandler()
        handler.setFormatter(default_formatter)
        log.addHandler(handler)

    # mostly disable third part logging
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)
    logging.getLogger('BitcoinRPC').setLevel(logging.INFO)
    logging.getLogger('aioupnp').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.CRITICAL)

    if verbose:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    if use_loggly:
        loggly_handler = get_loggly_handler(conf.settings['LOGGLY_TOKEN'])
        loggly_handler.setLevel(logging.ERROR)
        log.addHandler(loggly_handler)


async def start_daemon(settings: typing.Optional[typing.Dict] = None,
                       console_output: typing.Optional[bool] = True, verbose: typing.Optional[bool] = False,
                       data_dir: typing.Optional[str] = None, wallet_dir: typing.Optional[str] = None,
                       download_dir: typing.Optional[str] = None):
    # initialize the settings
    settings = settings or {}
    conf.initialize_settings(data_dir=data_dir, wallet_dir=wallet_dir, download_dir=download_dir)
    for k, v in settings.items():
        conf.settings.update({k, v}, data_types=(conf.TYPE_CLI,))

    setup_logging(console_output, verbose, conf.settings['share_usage_data'])

    log.debug('Final Settings: %s', conf.settings.get_current_settings_dict())
    log.info("Starting lbrynet-daemon from command line")

    daemon = Daemon()
    await daemon.start_listening()
    return daemon


async def start_daemon_with_cli_args(argv=None, data_dir: typing.Optional[str] = None,
                                     wallet_dir: typing.Optional[str] = None,
                                     download_dir: typing.Optional[str] = None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--http-auth", dest="useauth", action="store_true", default=False
    )
    parser.add_argument(
        '--quiet', dest='quiet', action="store_true",
        help='Disable all console output.'
    )
    parser.add_argument(
        '--verbose', dest="verbose", action="store_true", default=False

    )
    parser.add_argument(
        '--version', action="store_true",
        help='Show daemon version and quit'
    )

    args = parser.parse_args(argv)
    settings = {}
    if args.useauth:
        settings['use_auth_http'] = True

    if args.version:
        print(json_dumps_pretty(get_platform()))
        return
    try:
        daemon = await start_daemon(settings, not args.quiet, args.verbose, data_dir, wallet_dir, download_dir)
    except (OSError, asyncio.CancelledError):
        return
    try:
        await daemon.server.wait_closed()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await daemon.shutdown()


async def execute_command(method, params, data_dir: typing.Optional[str] = None,
                          wallet_dir: typing.Optional[str] = None, download_dir: typing.Optional[str] = None):
    conf.initialize_settings(data_dir=data_dir, wallet_dir=wallet_dir, download_dir=download_dir)
    api = None
    try:
        api = await LBRYAPIClient.get_client()
        resp = await api.call(method, params)
        print(json.dumps(resp, indent=2))
    except (ClientConnectorError, ConnectionError):
        print("Could not connect to daemon. Are you sure it's running?")
        return 1
    except JSONRPCException as err:
        print(json.dumps(err.error, indent=2))
        return 1
    finally:
        if api:
            await api.session.close()


def print_help():
    print(dedent("""
    NAME
       lbrynet - LBRY command line client.
    
    USAGE
       lbrynet [--data_dir=<blob and database directory>] [--wallet_dir=<wallet directory>]
               [--download_dir=<downloads directory>] <command> [<args>]

    EXAMPLES
        lbrynet start                                # starts the daemon and listens for jsonrpc commands
        lbrynet help                                 # display this message
        lbrynet help <command_name>                  # get help for a command(doesn't need the daemon to be running)
        lbrynet commands                             # list available commands
        lbrynet status                               # get the running status of the daemon
        lbrynet resolve what                         # resolve a name

        lbrynet --wallet_dir=~/wallet2 start         # start the daemon using an alternative wallet directory
        lbrynet --data_dir=~/lbry start              # start the daemon using an alternative data directory

        lbrynet --data_dir=~/lbry <command_name>     # run a command on a daemon using an alternative data directory,
                                                     # which can contain a full daemon_settings.yml config file.
                                                     # Note: since the daemon is what runs the wallet and
                                                     # downloads files, only the --data_dir setting is needed when
                                                     # running commands. The wallet_dir and download_dir would only
                                                     # by used when starting the daemon.
    """))


def print_help_for_command(command):
    fn = Daemon.callable_methods.get(command)
    if fn:
        print(dedent(fn.__doc__))
    else:
        print("Invalid command name")


def normalize_value(x, key=None):
    if not isinstance(x, str):
        return x
    if key in ('uri', 'channel_name', 'name', 'file_name', 'download_directory'):
        return x
    if x.lower() == 'true':
        return True
    if x.lower() == 'false':
        return False
    if x.isdigit():
        return int(x)
    return x


def remove_brackets(key):
    if key.startswith("<") and key.endswith(">"):
        return str(key[1:-1])
    return key


def set_kwargs(parsed_args):
    kwargs = {}
    for key, arg in parsed_args.items():
        k = None
        if arg is None:
            continue
        elif key.startswith("--") and remove_brackets(key[2:]) not in kwargs:
            k = remove_brackets(key[2:])
        elif remove_brackets(key) not in kwargs:
            k = remove_brackets(key)
        kwargs[k] = normalize_value(arg, k)
    return kwargs


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print_help()
        return 1

    dir_args = {}
    if len(argv) >= 2:
        dir_arg_keys = [
            'data_dir',
            'wallet_dir',
            'download_directory'
        ]

        for arg in argv:
            found_dir_arg = False
            for key in dir_arg_keys:
                if arg.startswith(f'--{key}='):
                    if key in dir_args:
                        print(f"Multiple values provided for '{key}' argument")
                        print_help()
                        return 1
                    dir_args[key] = os.path.expanduser(os.path.expandvars(arg.lstrip(f'--{key}=')))
                    found_dir_arg = True
            if not found_dir_arg:
                break
        argv = argv[len(dir_args):]

    data_dir = dir_args.get('data_dir')
    wallet_dir = dir_args.get('wallet_dir')
    download_dir = dir_args.get('download_directory')

    for k, v in dir_args.items():
        if not os.path.isdir(v):
            print(f"'{data_dir}' is not a directory, cannot use it for {k}")
            return 1

    method, args = argv[0], argv[1:]

    if method in ['help', '--help', '-h']:
        if len(args) == 1:
            print_help_for_command(args[0])
        else:
            print_help()
        return 0

    elif method in ['version', '--version', '-v']:
        print("lbrynet {lbrynet_version}".format(**get_platform()))
        return 0

    elif method == 'start':
        try:
            asyncio.run(start_daemon_with_cli_args(args, data_dir, wallet_dir, download_dir))
        except KeyboardInterrupt:
            pass
        return 0

    elif method not in Daemon.callable_methods:
        if method not in Daemon.deprecated_methods:
            print(f'{method} is not a valid command.')
            return 1

        new_method = Daemon.deprecated_methods[method].new_command
        if new_method is None:
            print(f"{method} is permanently deprecated and does not have a replacement command.")
            return 0

        print(f"{method} is deprecated, using {new_method}.")
        method = new_method

    fn = Daemon.callable_methods[method]
    parsed = docopt(fn.__doc__, args)
    params = set_kwargs(parsed)
    asyncio.run(execute_command(method, params, data_dir, wallet_dir, download_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
