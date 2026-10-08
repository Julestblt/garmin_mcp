import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import requests
from cryptography.fernet import Fernet


class TokenStore(Protocol):
    def load(self, user_id: uuid.UUID) -> str | None: ...
    def save(self, user_id: uuid.UUID, token_data: str) -> None: ...
    def delete(self, user_id: uuid.UUID) -> None: ...
    def exists(self, user_id: uuid.UUID) -> bool: ...


class FileTokenStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, user_id: uuid.UUID) -> Path:
        return self.directory / f'{user_id}.json'

    def load(self, user_id: uuid.UUID) -> str | None:
        path = self._path(user_id)
        return path.read_text() if path.exists() else None

    def save(self, user_id: uuid.UUID, token_data: str) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(user_id)
        path.write_text(token_data)
        path.chmod(0o600)

    def delete(self, user_id: uuid.UUID) -> None:
        self._path(user_id).unlink(missing_ok=True)

    def exists(self, user_id: uuid.UUID) -> bool:
        return self._path(user_id).exists()


class SupabaseDatabase:
    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip('/') + '/rest/v1'
        self.headers = {
            'apikey': service_key,
            'Authorization': f'Bearer {service_key}',
            'Content-Type': 'application/json',
        }

    def request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                data: Any = None, prefer: str | None = None) -> Any:
        headers = dict(self.headers)
        if prefer:
            headers['Prefer'] = prefer
        response = requests.request(method, self.url + '/' + path, params=params,
                                    json=data, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json() if response.content else None

    def count(self, path: str, *, params: dict[str, str] | None = None) -> int:
        headers = dict(self.headers)
        headers['Prefer'] = 'count=exact'
        headers['Range'] = '0-0'
        response = requests.get(self.url + '/' + path, params=params, headers=headers,
                                timeout=20)
        response.raise_for_status()
        content_range = response.headers.get('Content-Range', '*/0')
        return int(content_range.rsplit('/', 1)[1])


class EncryptedSupabaseTokenStore:
    def __init__(self, database: SupabaseDatabase, key: str):
        self.database = database
        self.cipher = Fernet(key.encode())

    def load(self, user_id: uuid.UUID) -> str | None:
        stored = self.load_versioned(user_id)
        return stored[0] if stored else None

    def load_versioned(self, user_id: uuid.UUID) -> tuple[str, int] | None:
        rows = self.database.request('GET', 'provider_secrets', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin', 'select': 'ciphertext,version'})
        if not rows:
            return None
        return self.cipher.decrypt(rows[0]['ciphertext'].encode()).decode(), rows[0]['version']

    def acquire(self, connection_id: uuid.UUID, owner: uuid.UUID) -> bool:
        return bool(self.database.request('POST', 'rpc/acquire_provider_lease', data={
            'p_connection_id': str(connection_id), 'p_owner': str(owner)}))

    def renew(self, connection_id: uuid.UUID, owner: uuid.UUID) -> bool:
        return bool(self.database.request('POST', 'rpc/renew_provider_lease', data={
            'p_connection_id': str(connection_id), 'p_owner': str(owner)}))

    def release(self, connection_id: uuid.UUID, owner: uuid.UUID) -> None:
        self.database.request('POST', 'rpc/release_provider_lease', data={
            'p_connection_id': str(connection_id), 'p_owner': str(owner)})

    def save_if_version(self, user_id: uuid.UUID, connection_id: uuid.UUID,
                        owner: uuid.UUID, version: int, token_data: str) -> int:
        next_version = self.database.request('POST', 'rpc/persist_provider_secret', data={
            'p_user_id': str(user_id), 'p_connection_id': str(connection_id),
            'p_owner': str(owner), 'p_version': version,
            'p_ciphertext': self.cipher.encrypt(token_data.encode()).decode()})
        if next_version is None:
            raise RuntimeError('provider_session_conflict')
        return int(next_version)

    def save(self, user_id: uuid.UUID, token_data: str) -> None:
        connections = self.database.request('GET', 'provider_connections', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin', 'select': 'id'})
        if not connections:
            raise RuntimeError('provider_connection_missing')
        self.save_if_connection(user_id, uuid.UUID(connections[0]['id']), token_data)

    def save_if_connection(self, user_id: uuid.UUID, connection_id: uuid.UUID,
                           token_data: str) -> None:
        stored = self.database.request('POST', 'rpc/store_provider_secret', data={
            'p_user_id': str(user_id), 'p_provider': 'garmin',
            'p_connection_id': str(connection_id),
            'p_ciphertext': self.cipher.encrypt(token_data.encode()).decode()})
        if not stored:
            raise RuntimeError('provider_connection_changed')

    def activate(self, user_id: uuid.UUID, expected_connection_id: uuid.UUID | None,
                 token_data: str, history_start_date: str) -> uuid.UUID:
        connection_id = self.database.request('POST', 'rpc/activate_garmin_connection', data={
            'p_user_id': str(user_id),
            'p_expected_connection_id': (str(expected_connection_id)
                                         if expected_connection_id else None),
            'p_ciphertext': self.cipher.encrypt(token_data.encode()).decode(),
            'p_oldest_date': history_start_date})
        if not connection_id:
            raise RuntimeError('provider_connection_changed')
        return uuid.UUID(connection_id)

    def delete(self, user_id: uuid.UUID) -> None:
        self.database.request('DELETE', 'provider_secrets', params={
            'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'})

    def exists(self, user_id: uuid.UUID) -> bool:
        return self.load(user_id) is not None


@dataclass(frozen=True)
class Challenge:
    id: uuid.UUID
    user_id: uuid.UUID
    state: dict[str, Any]


class SupabaseChallengeStore:
    def __init__(self, database: SupabaseDatabase, key: str, ttl_minutes: int = 10):
        self.database = database
        self.cipher = Fernet(key.encode())
        self.ttl = timedelta(minutes=ttl_minutes)

    def create(self, user_id: uuid.UUID, state: dict[str, Any]) -> uuid.UUID:
        challenge_id = uuid.uuid4()
        self.database.request('POST', 'auth_challenges', data={
            'id': str(challenge_id), 'user_id': str(user_id), 'provider': 'garmin',
            'encrypted_state': self.cipher.encrypt(json.dumps(state).encode()).decode(),
            'expires_at': (datetime.now(timezone.utc) + self.ttl).isoformat()})
        return challenge_id

    def consume(self, user_id: uuid.UUID, challenge_id: uuid.UUID) -> Challenge | None:
        rows = self.database.request('POST', 'rpc/consume_auth_challenge', data={
            'p_user_id': str(user_id), 'p_challenge_id': str(challenge_id)})
        if not rows:
            return None
        state = json.loads(self.cipher.decrypt(rows[0]['encrypted_state'].encode()))
        return Challenge(challenge_id, user_id, state)

    def load(self, user_id: uuid.UUID, challenge_id: uuid.UUID) -> Challenge | None:
        rows = self.database.request('GET', 'auth_challenges', params={
            'id': f'eq.{challenge_id}', 'user_id': f'eq.{user_id}',
            'provider': 'eq.garmin', 'expires_at': f'gt.{datetime.now(timezone.utc).isoformat()}',
            'select': 'encrypted_state'})
        if not rows:
            return None
        state = json.loads(self.cipher.decrypt(rows[0]['encrypted_state'].encode()))
        return Challenge(challenge_id, user_id, state)

    def delete(self, user_id: uuid.UUID, challenge_id: uuid.UUID) -> None:
        self.database.request('DELETE', 'auth_challenges', params={
            'id': f'eq.{challenge_id}', 'user_id': f'eq.{user_id}', 'provider': 'eq.garmin'})

    def expire(self) -> None:
        self.database.request('DELETE', 'auth_challenges', params={
            'expires_at': f'lt.{datetime.now(timezone.utc).isoformat()}'})
