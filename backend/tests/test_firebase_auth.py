import importlib
import sys
from datetime import datetime
from types import ModuleType
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from google.auth.exceptions import DefaultCredentialsError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.models.entities import User


PROJECT_ID = "test-firebase-project"
FIREBASE_UID = "firebase-user-123"
AUTH_TIME = 1_700_000_000
TOKEN = "sensitive-firebase-id-token"


@pytest.fixture(scope="module")
def firebase_auth_sdk():
    auth_sdk = ModuleType("firebase_admin.auth")
    auth_sdk.verify_id_token = Mock()

    class FirebaseError(Exception):
        pass

    class InvalidIdTokenError(FirebaseError):
        pass

    class RevokedIdTokenError(FirebaseError):
        pass

    class ExpiredIdTokenError(FirebaseError):
        pass

    class UserDisabledError(FirebaseError):
        pass

    class CertificateFetchError(FirebaseError):
        pass

    for name, error_type in (
        ("FirebaseError", FirebaseError),
        ("InvalidIdTokenError", InvalidIdTokenError),
        ("RevokedIdTokenError", RevokedIdTokenError),
        ("ExpiredIdTokenError", ExpiredIdTokenError),
        ("UserDisabledError", UserDisabledError),
        ("CertificateFetchError", CertificateFetchError),
    ):
        setattr(auth_sdk, name, error_type)

    exceptions_sdk = ModuleType("firebase_admin.exceptions")
    exceptions_sdk.FirebaseError = FirebaseError

    credentials_sdk = ModuleType("firebase_admin.credentials")
    credentials_sdk.Certificate = Mock(return_value=object())

    firebase_admin = ModuleType("firebase_admin")
    firebase_admin.__path__ = []
    firebase_admin.auth = auth_sdk
    firebase_admin.exceptions = exceptions_sdk
    firebase_admin.credentials = credentials_sdk
    firebase_admin.initialize_app = Mock(return_value=object())

    sdk_modules = {
        "firebase_admin": firebase_admin,
        "firebase_admin.auth": auth_sdk,
        "firebase_admin.exceptions": exceptions_sdk,
    }
    original_modules = {
        name: sys.modules.get(name)
        for name in sdk_modules
    }
    sys.modules.update(sdk_modules)
    try:
        auth_module = None
        try:
            auth_module = importlib.import_module("app.core.firebase_auth")
            auth_module.auth = auth_sdk
            auth_module.firebase_admin = firebase_admin
            auth_module._firebase_app = None
        except ModuleNotFoundError as exc:
            if exc.name != "app.core.firebase_auth":
                raise
        if auth_module is None:
            pytest.fail(
                "Missing app.core.firebase_auth; expected "
                "get_current_principal and verify_firebase_id_token",
                pytrace=False,
            )

        missing_exports = [
            name
            for name in ("get_current_principal", "verify_firebase_id_token")
            if not hasattr(auth_module, name)
        ]
        assert not missing_exports, (
            "app.core.firebase_auth is missing public exports: "
            + ", ".join(missing_exports)
        )
    finally:
        for name, original_module in original_modules.items():
            if original_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original_module

    return auth_module, auth_sdk, FirebaseError, InvalidIdTokenError, RevokedIdTokenError, CertificateFetchError


@pytest.fixture
def ready_auth(firebase_auth_sdk, monkeypatch):
    auth_module, auth_sdk, *error_types = firebase_auth_sdk
    monkeypatch.setattr(settings, "firebase_project_id", PROJECT_ID)
    auth_sdk.verify_id_token.reset_mock()
    auth_sdk.verify_id_token.side_effect = None
    auth_sdk.verify_id_token.return_value = {
        "uid": FIREBASE_UID,
        "auth_time": AUTH_TIME,
        "aud": PROJECT_ID,
        "iss": f"https://securetoken.google.com/{PROJECT_ID}",
    }
    return auth_module, auth_sdk, error_types


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    user = User(
        firebase_uid=FIREBASE_UID,
        email="user@example.com",
        email_verified=True,
    )
    session.add(user)
    session.commit()
    try:
        yield session, user
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _protected_app(auth_module, db_session):
    app = FastAPI()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    @app.get("/protected")
    def protected(principal=Depends(auth_module.get_current_principal)):
        auth_time = principal.auth_time
        if isinstance(auth_time, datetime):
            auth_time = int(auth_time.timestamp())
        return {
            "uid": principal.uid,
            "auth_time": auth_time,
            "user_id": str(principal.user.id),
        }

    return app


def _get_protected_response(auth_module, db_session, authorization=f"Bearer {TOKEN}"):
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    with TestClient(_protected_app(auth_module, db_session)) as client:
        return client.get("/protected", headers=headers)


def test_missing_authorization_returns_401(ready_auth, database):
    auth_module, auth_sdk, _ = ready_auth
    response = _get_protected_response(auth_module, database[0], authorization=None)

    assert response.status_code == 401
    auth_sdk.verify_id_token.assert_not_called()


@pytest.mark.parametrize(
    "authorization",
    ["Basic abc", "Bearer", "Bearer token with spaces", "Bearer\tabc"],
)
def test_malformed_or_non_bearer_authorization_returns_401(
    ready_auth, database, authorization
):
    auth_module, auth_sdk, _ = ready_auth
    response = _get_protected_response(
        auth_module,
        database[0],
        authorization=authorization,
    )

    assert response.status_code == 401
    auth_sdk.verify_id_token.assert_not_called()


def test_verified_token_resolves_local_user_and_principal(ready_auth, database):
    auth_module, auth_sdk, _ = ready_auth
    db_session, user = database

    response = _get_protected_response(auth_module, db_session)

    assert response.status_code == 200
    assert response.json() == {
        "uid": FIREBASE_UID,
        "auth_time": AUTH_TIME,
        "user_id": str(user.id),
    }
    auth_sdk.verify_id_token.assert_called_once_with(TOKEN, check_revoked=True)


def test_firebase_credentials_path_initializes_admin_with_service_account(
    ready_auth, monkeypatch
):
    auth_module, _, _ = ready_auth
    credential_path = "/run/secrets/firebase-admin.json"
    credential = object()
    certificate = auth_module.firebase_admin.credentials.Certificate
    certificate.return_value = credential
    initialize_app = auth_module.firebase_admin.initialize_app
    initialize_app.reset_mock()
    auth_module._firebase_app = None
    monkeypatch.setattr(settings, "firebase_credentials_path", credential_path)

    initialized_app = auth_module._get_firebase_app()

    certificate.assert_called_once_with(credential_path)
    initialize_app.assert_called_once_with(
        credential,
        options={"projectId": PROJECT_ID},
    )
    assert initialized_app is initialize_app.return_value


@pytest.mark.parametrize(
    ("error_index", "message"),
    [(1, "invalid token"), (2, "revoked token")],
)
def test_invalid_or_revoked_token_returns_401_without_leaking_token(
    ready_auth, database, error_index, message
):
    auth_module, auth_sdk, error_types = ready_auth
    error_type = error_types[error_index]
    auth_sdk.verify_id_token.side_effect = error_type(message)

    response = _get_protected_response(auth_module, database[0])

    assert response.status_code == 401
    assert TOKEN not in response.text


def test_firebase_verification_infrastructure_error_returns_503(ready_auth, database):
    auth_module, auth_sdk, error_types = ready_auth
    firebase_error = error_types[0]
    certificate_fetch_error = error_types[3]
    auth_sdk.verify_id_token.side_effect = certificate_fetch_error(
        "certificate service unavailable"
    )

    response = _get_protected_response(auth_module, database[0])

    assert response.status_code == 503
    assert TOKEN not in response.text

    auth_sdk.verify_id_token.side_effect = firebase_error("Firebase service unavailable")
    response = _get_protected_response(auth_module, database[0])
    assert response.status_code == 503
    assert TOKEN not in response.text


def test_missing_application_default_credentials_returns_503(ready_auth, database):
    auth_module, auth_sdk, _ = ready_auth
    auth_sdk.verify_id_token.side_effect = DefaultCredentialsError(
        "default credentials unavailable"
    )

    response = _get_protected_response(auth_module, database[0])

    assert response.status_code == 503
    assert response.json()["detail"] == "Firebase Admin credentials are not configured."
    assert TOKEN not in response.text


def test_missing_firebase_project_configuration_returns_503(
    ready_auth, database, monkeypatch
):
    auth_module, auth_sdk, _ = ready_auth
    monkeypatch.setattr(settings, "firebase_project_id", "")

    response = _get_protected_response(auth_module, database[0])

    assert response.status_code == 503
    auth_sdk.verify_id_token.assert_not_called()


@pytest.mark.parametrize(
    "claims",
    [
        {"auth_time": AUTH_TIME, "aud": PROJECT_ID},
        {"uid": FIREBASE_UID, "aud": PROJECT_ID},
        {
            "uid": FIREBASE_UID,
            "auth_time": AUTH_TIME,
            "aud": "different-firebase-project",
        },
    ],
    ids=["missing-uid", "missing-auth-time", "wrong-project"],
)
def test_incomplete_or_wrong_project_claims_fail_closed(
    ready_auth, database, claims
):
    auth_module, auth_sdk, _ = ready_auth
    auth_sdk.verify_id_token.return_value = claims

    response = _get_protected_response(auth_module, database[0])

    assert response.status_code == 401
    assert TOKEN not in response.text
