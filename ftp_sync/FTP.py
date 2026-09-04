import contextlib
import datetime
import logging
import hashlib
import json
import platform
import os
import posixpath

from contextlib import redirect_stdout
from dateutil import parser
from ftplib import FTP, error_perm, error_temp
from pathlib import Path
from tempfile import TemporaryFile

logger = logging.getLogger(__name__)

if platform.system() == 'Windows':
    FTP_SYNC_HOME = Path(str(os.environ.get('UserProfile'))) / "Documents" / "ftp_sync"
else:
    FTP_SYNC_HOME = Path(str(os.environ.get('HOME'))) / ".config" / "ftp_sync"
FTP_SYNC_CONFIG_PATH = FTP_SYNC_HOME / "ftp_sync.yaml"

class Patcher:
    def  to_remote(self, file):
        pass
    def from_remote(self, file):
        pass

class DESMumePatcher(Patcher):
    DESMUME_FOOTER = b'|<--Snip above here to create a raw sav by excluding this DeSmuME savedata footer:\x01\x00\x04\x00\x00\x00\x08\x00\x06\x00\x00\x00\x03\x00\x00\x00\x00\x00\x08\x00\x00\x00\x00\x00|-DESMUME SAVE-|'

    def from_remote(self, file):
        logger.info("Applying DESMume footer")
        return file.read() + self.DESMUME_FOOTER

    def to_remote(self, file):
        logger.info("Removing DESMume footer")
        data = file.read()
        return data[:len(data) - len(self.DESMUME_FOOTER)]

class FTPSync:

    LOCAL_TO_REMOTE = 0
    REMOTE_TO_LOCAL = 1
    DO_NOT_SYNC = 2
    MIN_SAFE_DRIFT = 600
    DATE_FORMAT = "%m_%d_%Y_%H_%M"

    def __init__(self, ftp_helper, backup_dir=FTP_SYNC_HOME / "backup", hash_db_path=FTP_SYNC_HOME / "hash_db.json"):
        if not os.path.exists(FTP_SYNC_HOME):
            os.makedirs(FTP_SYNC_HOME)
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)

        self.ftp_helper = ftp_helper
        self.backup_dir = Path(backup_dir)
        self.hash_db_path = Path(hash_db_path)
        if not self.hash_db_path.exists():
            self.hash_db = {'local': {}, 'remote': {}}
        else:
            try:
                with self.hash_db_path.open() as f:
                    loaded_hash_db = json.load(f)
                if not isinstance(loaded_hash_db, dict) or not all(
                    isinstance(loaded_hash_db.get(key), dict) for key in ('local', 'remote')
                ):
                    raise ValueError("hash database must contain local and remote mappings")
                self.hash_db = loaded_hash_db
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Ignoring invalid hash database %s: %s", self.hash_db_path, exc)
                self.hash_db = {'local': {}, 'remote': {}}
        if not os.path.exists(self.backup_dir):
            os.makedirs(self.backup_dir)
    
    def get_last_modified(self, path, remote=False):
        if not remote:
            if os.path.exists(path):
                return datetime.datetime.fromtimestamp(Path(path).stat().st_mtime)
            else:
                return datetime.datetime.fromtimestamp(0)
        else:
            last_modified = self.ftp_helper.last_modified(path)
            if last_modified is not None:
                return last_modified
            else:
                return datetime.datetime.fromtimestamp(0)

    def _get_previous_digest(self, path, remote=False):
        path = str(path)
        if remote:
            return self.hash_db['remote'].get(path)
        else:
            return self.hash_db['local'].get(path)

    def _set_previous_digest(self, path, digest, remote=False):
        path = str(path)
        if remote:
            self.hash_db['remote'][path] = digest
        else:
            self.hash_db['local'][path] = digest

    def _get_digest(self, path, remote=False, patcher=None):
        if remote:
            try:
                with self.ftp_helper.download_to_tempfile(path, patcher) as f:
                    return hashlib.md5(f.read()).hexdigest()
            except (error_perm, error_temp):
                return None
        else:
            if os.path.exists(path):
                with open(path, 'rb') as f:
                    return hashlib.md5(f.read()).hexdigest()
            else:
                return None

    def _get_sync_direction(self, local_path, remote_path, method="hash", patcher=None):
        if method == "hash":
            lp_previous_hash = self._get_previous_digest(local_path, remote=False)
            rp_previous_hash = self._get_previous_digest(remote_path, remote=True)
            lp_hash = self._get_digest(local_path, remote=False, patcher=patcher)
            rp_hash = self._get_digest(remote_path, remote=True, patcher=patcher)
            logger.debug(f'local: {lp_previous_hash} -> {lp_hash}    remote: {rp_previous_hash} -> {rp_hash}')
            if rp_hash is None and lp_hash is None:
                logger.info("Not syncing: neither path exists")
                return self.DO_NOT_SYNC
            elif rp_hash is None and lp_hash is not None:
                logger.info("Syncing local to remote: remote path does not exist")
                return self.LOCAL_TO_REMOTE
            elif rp_hash is not None and lp_hash is None:
                logger.info("Syncing remote to local: local path does not exist")
                return self.REMOTE_TO_LOCAL
            elif lp_previous_hash == rp_previous_hash:
                if rp_hash != rp_previous_hash and lp_hash == lp_previous_hash:
                    logger.info("Syncing remote to local: remote file updated")
                    return self.REMOTE_TO_LOCAL
                elif rp_hash == rp_previous_hash and lp_hash != lp_previous_hash:
                    logger.info("Syncing local to remote: local file updated")
                    return self.LOCAL_TO_REMOTE
                elif rp_hash == rp_previous_hash and lp_hash == lp_previous_hash:
                    logger.info("Not syncing: neither path updated")
                    return self.DO_NOT_SYNC
                else:
                    logger.info("Not syncing: both paths updated. Please manually sync with either the sync_to or sync_from command")
                    return self.DO_NOT_SYNC
            else:
                logger.info("Not syncing: previous hashes are different. Please manually sync with either the sync_to or sync_from command")
                return self.DO_NOT_SYNC
        else:
            lp_mtime = self.get_last_modified(local_path, remote=False)
            rp_mtime = self.get_last_modified(remote_path, remote=True)
            logger.info(f"Local: {str(lp_mtime)}    Remote: {str(rp_mtime)}: {abs((lp_mtime - rp_mtime).seconds)}")
            if lp_mtime > rp_mtime:
                delta = lp_mtime - rp_mtime
            else:
                delta = rp_mtime - lp_mtime
            if abs(delta.seconds) > self.MIN_SAFE_DRIFT:
                if lp_mtime > rp_mtime:
                    return self.LOCAL_TO_REMOTE
                else:
                    return self.REMOTE_TO_LOCAL
            else:
                return self.DO_NOT_SYNC

    def backup(self, path, remote=False):
        backup_filename = (self.get_last_modified(path, remote=remote).strftime(self.DATE_FORMAT) + "___" + datetime.datetime.now().strftime(self.DATE_FORMAT))
        path = str(path)
        if not path.startswith('/'):
            path = str(Path(path).resolve())
        for d in path.replace('\\', '/').split('/')[1:]:
            backup_filename += "___" + d
        local_path = self.backup_dir / backup_filename
        logger.info(f"Backing up {remote=} {path} to {backup_filename}")
        if remote:
            if self.ftp_helper.file_exists(path):
                self.ftp_helper.download_file(path, local_path)
            else:
                logger.info("Remote path doesn't exist... No use in backing up nothing!")
        else:
            with open(local_path, 'w+b') as dest:
                if os.path.exists(path):
                    with open(path, 'rb') as src:
                        dest.write(src.read())
                else:
                    logger.info("Local path doesn't exist... No use in backing up nothing!")

    def sync_to(self, local_path, remote_path, patcher=None, delete=False):
        digest = self._get_digest(local_path, remote=False)
        self.backup(remote_path, remote=True)
        self.ftp_helper.upload_file(local_path, remote_path, patcher=patcher)
        self._set_previous_digest(local_path, digest, remote=False)
        self._set_previous_digest(remote_path, digest, remote=True)

    def _sync_directory_file(self, local_path, remote_path, method, patcher=None):
        if method == "sync":
            self.sync(local_path, remote_path, patcher=patcher)
        elif method == "sync_to":
            self.sync_to(local_path, remote_path, patcher=patcher)
        else:
            self.sync_from(local_path, remote_path, patcher=patcher)

    def sync_directory(self, local_path, remote_path, method="sync", patcher=None, delete=False):
        """Synchronize files below two existing directory roots by relative path."""
        local_root = Path(local_path)
        remote_root = FTPHelper.normalize_remote_path(remote_path)
        local_entries = {
            path.relative_to(local_root).as_posix(): ("directory" if path.is_dir() else "file")
            for path in local_root.rglob("*")
        }
        remote_entries = self.ftp_helper.list_tree(remote_root)
        conflicts = 0
        all_paths = sorted(set(local_entries) | set(remote_entries))
        for relative_path in all_paths:
            local_type = local_entries.get(relative_path)
            remote_type = remote_entries.get(relative_path)
            if local_type and remote_type and local_type != remote_type:
                logger.warning("Not syncing %s: local and remote types differ", relative_path)
                conflicts += 1
                continue
            if local_type == "directory" or remote_type == "directory":
                continue
            local_file = local_root / relative_path
            remote_file = posixpath.join(remote_root, relative_path)
            if local_type and remote_type:
                self._sync_directory_file(local_file, remote_file, method, patcher=patcher)
            elif method == "sync_to" and local_type:
                self.sync_to(local_file, remote_file, patcher=patcher)
            elif method == "sync_to" and remote_type and delete:
                logger.info("Removing remote file %s", remote_file)
                self.ftp_helper.delete_file(remote_file)
            elif method == "sync_from" and remote_type:
                self.sync_from(local_file, remote_file, patcher=patcher)
            elif method == "sync_from" and local_type and delete:
                logger.info("Removing local file %s", local_file)
                local_file.unlink()
            elif method == "sync":
                previous_local = self._get_previous_digest(str(local_file), remote=False)
                previous_remote = self._get_previous_digest(remote_file, remote=True)
                if local_type and not remote_type:
                    if delete and previous_remote is not None and previous_remote == previous_local:
                        logger.info("Removing remote file %s", remote_file)
                        self.ftp_helper.delete_file(remote_file)
                    else:
                        self.sync_to(local_file, remote_file, patcher=patcher)
                elif remote_type and not local_type:
                    if delete and previous_local is not None and previous_local == previous_remote:
                        logger.info("Removing local file %s", local_file)
                        local_file.unlink()
                    else:
                        self.sync_from(local_file, remote_file, patcher=patcher)
        return conflicts

    def sync_from(self, local_path, remote_path, patcher=None, delete=False):
        self.backup(local_path, remote=False)
        self.ftp_helper.download_file(remote_path, local_path, patcher=patcher)
        digest = self._get_digest(local_path, remote=False)
        self._set_previous_digest(local_path, digest, remote=False)
        self._set_previous_digest(remote_path, digest, remote=True)

    def sync(self, local_path, remote_path, patcher=None, delete=False):
        if Path(local_path).is_dir() or self.ftp_helper.is_directory(remote_path):
            if not Path(local_path).is_dir() or not self.ftp_helper.is_directory(remote_path):
                raise ValueError("local_path and remote_path must both be directories")
            return self.sync_directory(local_path, remote_path, method="sync", patcher=patcher, delete=delete)
        sync_direction = self._get_sync_direction(local_path, remote_path, patcher=patcher)
        if sync_direction == self.LOCAL_TO_REMOTE:
            self.sync_to(local_path, remote_path, patcher=patcher)
        elif sync_direction == self.REMOTE_TO_LOCAL:
            self.sync_from(local_path, remote_path, patcher=patcher)

    def __del__(self):
        hash_db_path = getattr(self, 'hash_db_path', None)
        hash_db = getattr(self, 'hash_db', None)
        if hash_db_path is None or hash_db is None:
            return
        try:
            hash_db_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = hash_db_path.with_suffix(hash_db_path.suffix + '.tmp')
            with temporary_path.open('w') as f:
                json.dump(hash_db, f)
            temporary_path.replace(hash_db_path)
        except OSError as exc:
            logger.warning("Could not save hash database %s: %s", hash_db_path, exc)

class FTPHelper:
    def __init__(self, hostname, port=21, user="anonymous", password="", connect_timeout=5):
        self.ftp_connection = FTP()
        self.ftp_connection.connect(host=hostname, port=port, timeout=connect_timeout)
        self.ftp_connection.login(user=user, passwd=password)
        if self.ftp_connection.sock is not None:
            self.ftp_connection.sock.settimeout(None)

    @staticmethod
    def normalize_remote_path(path):
        path = str(path).replace('\\', '/')
        return posixpath.normpath(path)

    def is_directory(self, remote_path):
        remote_path = self.normalize_remote_path(remote_path)
        try:
            self.ftp_connection.cwd(remote_path)
            return True
        except (error_perm, error_temp):
            return False

    def file_exists(self, remote_path):
        remote_path = self.normalize_remote_path(remote_path)
        try:
            return self.ftp_connection.size(remote_path) is not None
        except (error_perm, error_temp):
            try:
                self.ftp_connection.sendcmd(f"MDTM {remote_path}")
                return True
            except (error_perm, error_temp):
                return False

    def list_tree(self, remote_root):
        """Return relative remote paths and their types using MLSD."""
        remote_root = self.normalize_remote_path(remote_root)
        entries = {}

        def visit(directory, relative=""):
            try:
                listing = self.ftp_connection.mlsd(directory)
            except AttributeError as exc:
                raise RuntimeError("The FTP server must support MLSD for directory sync") from exc
            for name, facts in listing:
                if name in (".", ".."):
                    continue
                child_relative = posixpath.join(relative, name) if relative else name
                child_path = posixpath.join(directory, name)
                entry_type = facts.get("type")
                if entry_type == "dir":
                    entries[child_relative] = "directory"
                    visit(child_path, child_relative)
                elif entry_type == "file":
                    entries[child_relative] = "file"

        visit(remote_root)
        return entries

    def make_parent_dirs(self, remote_path):
        remote_path = self.normalize_remote_path(remote_path)
        parent = posixpath.dirname(remote_path)
        if not parent:
            return
        current = "/" if remote_path.startswith("/") else ""
        for part in parent.strip("/").split("/"):
            if not part:
                continue
            current = posixpath.join(current, part)
            try:
                self.ftp_connection.cwd(current)
            except (error_perm, error_temp):
                self.ftp_connection.mkd(current)

    def close(self):
        ftp_connection = getattr(self, 'ftp_connection', None)
        if ftp_connection is None or ftp_connection.sock is None:
            return
        try:
            ftp_connection.quit()
        except (AttributeError, OSError, EOFError):
            ftp_connection.close()

    def upload_file(self, local_path, remote_path, patcher=None):
        logger.info(f"Uploading {local_path} to {remote_path}")
        self.make_parent_dirs(remote_path)
        if patcher is not None:
            with open(local_path, 'rb') as lf:
                with TemporaryFile() as f:
                    f.write(patcher.to_remote(lf))
                    f.seek(0)
                    self.ftp_connection.storbinary(f"STOR {remote_path}", f)
        else:
            with open(local_path, "rb") as f:
                self.ftp_connection.storbinary(f"STOR {remote_path}", f)

    def download_file(self, remote_path, local_path, patcher=None):
        logger.info(f"Downloading {remote_path} to {local_path}")
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        if patcher is not None:
            with TemporaryFile() as f:
                self.ftp_connection.retrbinary(f"RETR {remote_path}", f.write)
                f.seek(0)
                with open(local_path, "w+b") as lf:
                    lf.write(patcher.from_remote(f))
        else:
            with open(local_path, "w+b") as f:
                self.ftp_connection.retrbinary(f"RETR {remote_path}", f.write)

    @contextlib.contextmanager
    def download_to_tempfile(self, remote_path, patcher=None):
        f = TemporaryFile()
        self.ftp_connection.retrbinary(f"RETR {remote_path}", f.write)
        if patcher is not None:
            logger.debug('Applying patch before taking digest')
            f.seek(0)
            data = patcher.from_remote(f)
            f.seek(0)
            f.write(data)
        f.seek(0)
        yield f
        f.close()

    def copy_file(self, path, new_path):
        with TemporaryFile() as t:
            self.ftp_connection.retrbinary(f"RETR {path}", t.write)
            t.seek(0)
            self.ftp_connection.storbinary(f"STOR {new_path}", t)

    def delete_file(self, path):
        self.ftp_connection.sendcmd(f"DELE {path}")

    def dir(self):
        with TemporaryFile("w+") as t:
            with redirect_stdout(t):
                self.ftp_connection.dir()
            t.seek(0)
            return t.read()

    def last_modified(self, remote_path):
        def line_filename(line):
            t = line.split()[7]
            return line[line.find(t) + len(t) :].strip()

        # Try using the built in command first
        try:
            return parser.parse(self.ftp_connection.sendcmd(f"MDTM {remote_path}"))

        # Otherwise grab from the cwd output
        except (error_perm, error_temp):
            try:
                remote_path = self.normalize_remote_path(remote_path)
                logger.debug(remote_path)
                self.ftp_connection.cwd(posixpath.dirname(remote_path))
            except (error_perm, error_temp):
                logging.warning(f"Path {remote_path} does not exist on remote")
                return None
            pwd = self.dir()
            logger.debug(pwd)
            for line in pwd.split("\n"):
                if line and line_filename(line) == posixpath.basename(remote_path):
                    return parser.parse(" ".join(line.split()[5:8]))

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
