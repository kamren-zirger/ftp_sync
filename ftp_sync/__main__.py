import anyconfig
import click
import logging
import os
from pathlib import Path

if __package__:
    from . import FTP
else:
    import FTP

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

patcher_dict = {"desmume": FTP.DESMumePatcher}


def _build_patcher(value):
    if not isinstance(value, dict) or 'name' not in value:
        raise click.ClickException("patcher must contain a name")
    options = value.get('options', {})
    if not isinstance(options, dict):
        raise click.ClickException("patcher options must be a mapping")
    try:
        return patcher_dict[value['name']](**options)
    except KeyError as exc:
        raise click.ClickException(f"unknown patcher: {value['name']}") from exc


def _build_extension_transformations(pair):
    values = pair.get('extension_transformations', [])
    if not isinstance(values, list):
        raise click.ClickException("extension_transformations must be a list")
    transformations = []
    local_extensions = set()
    remote_extensions = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise click.ClickException(f"extension transformation {index} must be a mapping")
        local = value.get('local')
        remote = value.get('remote')
        if (not isinstance(local, str) or not local.startswith('.') or len(local) == 1 or
                not isinstance(remote, str) or not remote.startswith('.') or len(remote) == 1):
            raise click.ClickException(
                f"extension transformation {index} requires non-empty local and remote suffixes")
        if '/' in local or '\\' in local or '/' in remote or '\\' in remote:
            raise click.ClickException(f"extension transformation {index} must contain suffixes only")
        if local in local_extensions or remote in remote_extensions:
            raise click.ClickException(f"duplicate extension transformation at index {index}")
        local_extensions.add(local)
        remote_extensions.add(remote)
        rule_patcher = _build_patcher(value['patcher']) if 'patcher' in value else None
        transformations.append(FTP.ExtensionTransformation(local, remote, rule_patcher))
    return transformations


def _parse_pair_to_kwargs(pair):
    if not isinstance(pair, dict) or 'local_path' not in pair or 'remote_path' not in pair:
        raise click.ClickException("each sync pair requires local_path and remote_path")
    try:
        local_path = Path(pair['local_path'])
        remote_path = FTP.FTPHelper.normalize_remote_path(pair['remote_path'])
    except TypeError as exc:
        raise click.ClickException("local_path and remote_path must be strings") from exc
    if not remote_path.startswith('/'):
        raise click.ClickException("remote_path must be an absolute FTP path")
    kwargs = {'local_path': local_path, 'remote_path': remote_path,
              'delete': bool(pair.get('delete', False)),
              'extension_transformations': _build_extension_transformations(pair)}
    if 'patcher' in pair:
        kwargs['patcher'] = _build_patcher(pair['patcher'])
    return kwargs


def _load_config(config_file):
    if not os.path.exists(config_file):
        raise click.ClickException(f"Config file does not exist: {config_file}")
    config = anyconfig.load(config_file)
    if not isinstance(config, dict):
        raise click.ClickException("config must be a mapping")
    if not isinstance(config.get('hostname'), str) or not config['hostname']:
        raise click.ClickException("config requires a non-empty hostname")
    port = config.get('port', 21)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise click.ClickException("port must be an integer between 1 and 65535")
    connect_timeout = config.get('connect_timeout', 5)
    if not isinstance(connect_timeout, (int, float)) or connect_timeout <= 0:
        raise click.ClickException("connect_timeout must be a positive number of seconds")
    if not isinstance(config.get('sync'), dict) or not config['sync']:
        raise click.ClickException("config requires a non-empty sync mapping")
    return config


def _get_pair(config, name):
    pair = config['sync'].get(name)
    if pair is None:
        raise click.ClickException(f"no sync pair named {name}")
    return pair


def _run_pair(sync, pair, method):
    kwargs = _parse_pair_to_kwargs(pair)
    local_path = kwargs['local_path']
    remote_path = kwargs['remote_path']
    remote_is_directory = sync.ftp_helper.is_directory(remote_path)
    local_is_directory = local_path.is_dir()
    if local_is_directory or remote_is_directory:
        if not local_is_directory or not remote_is_directory:
            raise click.ClickException("local_path and remote_path must both be directories or both be files")
        return sync.sync_directory(method=method, **kwargs)
    if kwargs['extension_transformations']:
        raise click.ClickException("extension_transformations require a directory pair")
    if method == 'sync':
        return sync.sync(**kwargs)
    if method == 'sync_to':
        return sync.sync_to(**kwargs)
    return sync.sync_from(**kwargs)


def _run(config, name, method):
    helper = FTP.FTPHelper(hostname=config['hostname'], port=config.get('port', 21),
                           connect_timeout=config.get('connect_timeout', 5))
    sync = FTP.FTPSync(helper)
    conflicts = _run_pair(sync, _get_pair(config, name), method) or 0
    if conflicts:
        raise click.ClickException(f"{conflicts} sync conflict(s) encountered")


def _run_all(config, method):
    helper = FTP.FTPHelper(hostname=config['hostname'], port=config.get('port', 21),
                           connect_timeout=config.get('connect_timeout', 5))
    sync = FTP.FTPSync(helper)
    conflicts = 0
    for name, pair in config['sync'].items():
        logger.info(f"{method}: {name}")
        conflicts += _run_pair(sync, pair, method) or 0
    if conflicts:
        raise click.ClickException(f"{conflicts} sync conflict(s) encountered")


@click.group()
@click.option('-c', '--config-file', type=str, default=FTP.FTP_SYNC_CONFIG_PATH,
              help="Yaml or json config file defining connection and sync pair settings")
@click.option('-d', '--debug', is_flag=True)
@click.pass_context
def main(ctx, config_file, debug):
    if debug:
        logger.setLevel(logging.DEBUG)
        FTP.logger.setLevel(logging.DEBUG)
        logging.basicConfig(level=logging.DEBUG)
    ctx.config = _load_config(config_file)


@main.command(help="Automatically sync a specified sync pair.")
@click.option('-n', '--name', type=str, required=True, help="Sync pair name")
@click.pass_context
def sync(ctx, name):
    _run(ctx.parent.config, name, 'sync')


@main.command(help="Automatically sync all sync pairs.")
@click.pass_context
def sync_all(ctx):
    _run_all(ctx.parent.config, 'sync')


@main.command(help="Sync all sync pairs local to remote.")
@click.pass_context
def sync_all_to(ctx):
    _run_all(ctx.parent.config, 'sync_to')


@main.command(help="Sync all sync pairs remote to local.")
@click.pass_context
def sync_all_from(ctx):
    _run_all(ctx.parent.config, 'sync_from')


@main.command(help="Sync a pair local to remote.")
@click.option('-n', '--name', type=str, required=True, help="Sync pair name")
@click.pass_context
def sync_to(ctx, name):
    _run(ctx.parent.config, name, 'sync_to')


@main.command(help="Sync a pair remote to local.")
@click.option('-n', '--name', type=str, required=True, help="Sync pair name")
@click.pass_context
def sync_from(ctx, name):
    _run(ctx.parent.config, name, 'sync_from')


if __name__ == "__main__":
    main()
