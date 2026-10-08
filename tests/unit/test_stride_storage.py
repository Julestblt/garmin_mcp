import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from garmin_mcp.stride_storage import EncryptedSupabaseTokenStore, FileTokenStore, SupabaseChallengeStore


class MemoryDatabase:
    def __init__(self):
        self.secrets = {}
        self.challenges = {}
        self.connections = {}

    def request(self, method, path, *, params=None, data=None, prefer=None):
        if path == 'provider_secrets':
            key = (data['user_id'], data['provider']) if data else (params['user_id'][3:], 'garmin')
            if method == 'POST':
                self.secrets[key] = data['ciphertext']
            if method == 'DELETE':
                self.secrets.pop(key, None)
            if method == 'GET':
                return [{'ciphertext': self.secrets[key]}] if key in self.secrets else []
        if path == 'provider_connections':
            user_id = params['user_id'][3:]
            return [{'id': self.connections.setdefault(user_id, str(uuid.uuid4()))}]
        if path == 'rpc/store_provider_secret':
            user_id = data['p_user_id']
            if self.connections[user_id] != data['p_connection_id']:
                return False
            self.secrets[(user_id, 'garmin')] = data['p_ciphertext']
            return True
        if path == 'auth_challenges':
            if method == 'POST':
                self.challenges[data['id']] = data
                return None
            challenge = self.challenges.get(params['id'][3:])
            if method == 'GET' and challenge and challenge['user_id'] == params['user_id'][3:]:
                if datetime.fromisoformat(challenge['expires_at']) > datetime.now(timezone.utc):
                    return [{'encrypted_state': challenge['encrypted_state']}]
            return []
        if path == 'rpc/consume_auth_challenge':
            challenge = self.challenges.get(data['p_challenge_id'])
            if challenge and challenge['user_id'] == data['p_user_id'] and datetime.fromisoformat(challenge['expires_at']) > datetime.now(timezone.utc):
                del self.challenges[data['p_challenge_id']]
                return [{'encrypted_state': challenge['encrypted_state']}]
            return []
        return None


def test_file_token_store_isolates_users(tmp_path: Path):
    store = FileTokenStore(tmp_path / 'tokens')
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    store.save(user_a, 'secret-a')
    assert store.load(user_a) == 'secret-a'
    assert store.load(user_b) is None
    assert store._path(user_a).stat().st_mode & 0o077 == 0
    store.delete(user_a)
    assert not store.exists(user_a)


def test_encrypted_tokens_are_user_scoped_and_deleted():
    database = MemoryDatabase()
    store = EncryptedSupabaseTokenStore(database, Fernet.generate_key().decode())
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    store.save(user_a, 'secret-a')
    store.save(user_b, 'secret-b')
    assert 'secret-a' not in next(iter(database.secrets.values()))
    assert store.load(user_a) == 'secret-a'
    assert store.load(user_b) == 'secret-b'
    old_connection = uuid.UUID(database.connections[str(user_b)])
    database.connections[str(user_b)] = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match='provider_connection_changed'):
        store.save_if_connection(user_b, old_connection, 'stale-token')
    assert store.load(user_b) == 'secret-b'
    store.delete(user_a)
    assert store.load(user_a) is None
    assert store.load(user_b) == 'secret-b'


def test_challenge_ownership_and_single_use():
    database = MemoryDatabase()
    store = SupabaseChallengeStore(database, Fernet.generate_key().decode())
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    challenge_id = store.create(user_a, {'flow': 'ios'})
    assert store.load(user_a, challenge_id).state == {'flow': 'ios'}
    assert store.load(user_b, challenge_id) is None
    assert store.consume(user_b, challenge_id) is None
    assert store.consume(user_a, challenge_id).state == {'flow': 'ios'}
    assert store.consume(user_a, challenge_id) is None


def test_expired_challenge_cannot_be_loaded_or_consumed():
    database = MemoryDatabase()
    store = SupabaseChallengeStore(database, Fernet.generate_key().decode())
    user_id = uuid.uuid4()
    challenge_id = store.create(user_id, {'flow': 'ios'})
    database.challenges[str(challenge_id)]['expires_at'] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert store.load(user_id, challenge_id) is None
    assert store.consume(user_id, challenge_id) is None
