import tempfile
import unittest
from pathlib import Path

from ftp_sync.FTP import ComposedPatcher, ExtensionTransformation, FTPSync, Patcher


class MarkerPatcher(Patcher):
    def __init__(self, marker):
        self.marker = marker

    def to_remote(self, file):
        return file.read() + self.marker

    def from_remote(self, file):
        data = file.read()
        if not data.endswith(self.marker):
            raise AssertionError("patcher order was incorrect")
        return data[:-len(self.marker)]


class FakeFTPHelper:
    def __init__(self, entries=None):
        self.entries = entries or {}
        self.uploads = []

    def list_tree(self, remote_root):
        return self.entries

    def is_directory(self, remote_path):
        return True

    def file_exists(self, remote_path):
        return False

    def last_modified(self, remote_path):
        return None

    def upload_file(self, local_path, remote_path, patcher=None):
        self.uploads.append((str(local_path), remote_path, patcher))


class ExtensionTransformationTests(unittest.TestCase):
    def test_suffix_mapping_and_composition(self):
        rule = ExtensionTransformation('.srm', '.sav', MarkerPatcher(b'R'))
        self.assertEqual(rule.local_to_remote('nested/game.srm'), 'nested/game.sav')
        self.assertEqual(rule.remote_to_local('nested/game.sav'), 'nested/game.srm')

        patcher = ComposedPatcher((MarkerPatcher(b'G'), rule.patcher))
        self.assertEqual(patcher.to_remote(type('File', (), {'read': lambda self: b'x'})()), b'xGR')

    def test_directory_upload_uses_transformed_remote_path(self):
        with tempfile.TemporaryDirectory() as directory:
            local_root = Path(directory)
            (local_root / 'nested').mkdir()
            (local_root / 'nested' / 'game.srm').write_bytes(b'game')
            helper = FakeFTPHelper()
            sync = FTPSync(helper, backup_dir=local_root / 'backup',
                           hash_db_path=local_root / 'hash.json')
            rule = ExtensionTransformation('.srm', '.sav')

            sync.sync_directory(local_root, '/remote', method='sync_to',
                                extension_transformations=[rule])

            self.assertEqual(helper.uploads[0][1], '/remote/nested/game.sav')

    def test_collisions_are_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            local_root = Path(directory)
            (local_root / 'game.srm').write_bytes(b'srm')
            (local_root / 'game.sav').write_bytes(b'sav')
            helper = FakeFTPHelper()
            sync = FTPSync(helper, backup_dir=local_root / 'backup',
                           hash_db_path=local_root / 'hash.json')

            with self.assertRaises(ValueError):
                sync.sync_directory(
                    local_root,
                    '/remote',
                    method='sync_to',
                    extension_transformations=[ExtensionTransformation('.srm', '.sav')],
                )
            self.assertEqual(helper.uploads, [])


if __name__ == '__main__':
    unittest.main()
