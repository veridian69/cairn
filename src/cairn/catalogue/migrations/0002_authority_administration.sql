CREATE TABLE principals (
    principal_id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT ck_principals_principal_id CHECK (
        length(principal_id) = 36
        AND principal_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND principal_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_principals_kind CHECK (kind IN ('human', 'workload')),
    CONSTRAINT ck_principals_label CHECK (
        length(label) BETWEEN 1 AND 63
        AND substr(label, 1, 1) GLOB '[a-z]'
        AND substr(label, -1, 1) GLOB '[a-z0-9]'
        AND label NOT GLOB '*[^a-z0-9-]*'
    ),
    CONSTRAINT ck_principals_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE UNIQUE INDEX uq_principals_label ON principals(label);

CREATE TABLE credentials (
    credential_id TEXT NOT NULL PRIMARY KEY,
    principal_id TEXT NOT NULL,
    verifier BLOB NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    CONSTRAINT fk_credentials_principal FOREIGN KEY (principal_id)
        REFERENCES principals (principal_id),
    CONSTRAINT ck_credentials_credential_id CHECK (
        length(credential_id) = 36
        AND credential_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND credential_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_credentials_verifier CHECK (
        typeof(verifier) = 'blob' AND length(verifier) = 32
    ),
    CONSTRAINT ck_credentials_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_credentials_expires_at CHECK (
        expires_at IS NULL
        OR (
            length(expires_at) = 27
            AND expires_at GLOB '????-??-??T??:??:??.??????Z'
            AND expires_at NOT GLOB '*[^0-9TZ:.-]*'
        )
    )
) STRICT;

CREATE TABLE grants (
    grant_id TEXT NOT NULL PRIMARY KEY,
    principal_id TEXT NOT NULL,
    realm_id TEXT NOT NULL,
    scope_segments TEXT NOT NULL,
    operations TEXT NOT NULL,
    read_clearance TEXT NOT NULL,
    write_classifications TEXT NOT NULL,
    delegable_operations TEXT,
    issued_by TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_grants_principal FOREIGN KEY (principal_id)
        REFERENCES principals (principal_id),
    CONSTRAINT fk_grants_realm FOREIGN KEY (realm_id)
        REFERENCES realms (realm_id),
    CONSTRAINT fk_grants_issued_by FOREIGN KEY (issued_by)
        REFERENCES principals (principal_id),
    CONSTRAINT ck_grants_grant_id CHECK (
        length(grant_id) = 36
        AND grant_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND grant_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_grants_scope_segments CHECK (
        json_valid(scope_segments)
        AND json_type(scope_segments) = 'array'
        AND json_array_length(scope_segments) <= 16
    ),
    CONSTRAINT ck_grants_operations CHECK (
        json_valid(operations) AND json_type(operations) = 'array'
    ),
    CONSTRAINT ck_grants_read_clearance CHECK (
        read_clearance IN ('public', 'internal', 'restricted')
    ),
    CONSTRAINT ck_grants_write_classifications CHECK (
        json_valid(write_classifications)
        AND json_type(write_classifications) = 'array'
    ),
    CONSTRAINT ck_grants_delegable_operations_shape CHECK (
        delegable_operations IS NULL
        OR (
            json_valid(delegable_operations)
            AND json_type(delegable_operations) = 'array'
        )
    ),
    CONSTRAINT ck_grants_delegable_operations_presence CHECK (
        (delegable_operations IS NULL) = (operations NOT LIKE '%"grant-manage"%')
    ),
    CONSTRAINT ck_grants_delegable_operations_no_grant_manage CHECK (
        delegable_operations IS NULL
        OR delegable_operations NOT LIKE '%"grant-manage"%'
    ),
    CONSTRAINT ck_grants_expires_at CHECK (
        expires_at IS NULL
        OR (
            length(expires_at) = 27
            AND expires_at GLOB '????-??-??T??:??:??.??????Z'
            AND expires_at NOT GLOB '*[^0-9TZ:.-]*'
        )
    ),
    CONSTRAINT ck_grants_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE TABLE credential_revocations (
    credential_id TEXT NOT NULL PRIMARY KEY,
    revoked_at TEXT NOT NULL,
    revoked_by TEXT,
    reason_code TEXT NOT NULL,
    CONSTRAINT fk_credential_revocations_credential FOREIGN KEY (credential_id)
        REFERENCES credentials (credential_id),
    CONSTRAINT fk_credential_revocations_revoked_by FOREIGN KEY (revoked_by)
        REFERENCES principals (principal_id),
    CONSTRAINT ck_credential_revocations_revoked_at CHECK (
        length(revoked_at) = 27
        AND revoked_at GLOB '????-??-??T??:??:??.??????Z'
        AND revoked_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_credential_revocations_reason_code CHECK (
        length(reason_code) BETWEEN 1 AND 63
        AND substr(reason_code, 1, 1) GLOB '[a-z]'
        AND substr(reason_code, -1, 1) GLOB '[a-z0-9]'
        AND reason_code NOT GLOB '*[^a-z0-9_]*'
    )
) STRICT;

CREATE TABLE grant_revocations (
    grant_id TEXT NOT NULL PRIMARY KEY,
    revoked_at TEXT NOT NULL,
    revoked_by TEXT,
    reason_code TEXT NOT NULL,
    CONSTRAINT fk_grant_revocations_grant FOREIGN KEY (grant_id)
        REFERENCES grants (grant_id),
    CONSTRAINT fk_grant_revocations_revoked_by FOREIGN KEY (revoked_by)
        REFERENCES principals (principal_id),
    CONSTRAINT ck_grant_revocations_revoked_at CHECK (
        length(revoked_at) = 27
        AND revoked_at GLOB '????-??-??T??:??:??.??????Z'
        AND revoked_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_grant_revocations_reason_code CHECK (
        length(reason_code) BETWEEN 1 AND 63
        AND substr(reason_code, 1, 1) GLOB '[a-z]'
        AND substr(reason_code, -1, 1) GLOB '[a-z0-9]'
        AND reason_code NOT GLOB '*[^a-z0-9_]*'
    )
) STRICT;

CREATE TRIGGER trg_principals_no_update
BEFORE UPDATE ON principals
BEGIN
    SELECT RAISE(ABORT, 'immutable_principal');
END;

CREATE TRIGGER trg_principals_no_delete
BEFORE DELETE ON principals
BEGIN
    SELECT RAISE(ABORT, 'immutable_principal');
END;

CREATE TRIGGER trg_credentials_no_update
BEFORE UPDATE ON credentials
BEGIN
    SELECT RAISE(ABORT, 'immutable_credential');
END;

CREATE TRIGGER trg_credentials_no_delete
BEFORE DELETE ON credentials
BEGIN
    SELECT RAISE(ABORT, 'immutable_credential');
END;

CREATE TRIGGER trg_grants_workload_expiry
BEFORE INSERT ON grants
WHEN NEW.expires_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workload_grant_requires_expiry')
    WHERE (
        SELECT kind FROM principals WHERE principal_id = NEW.principal_id
    ) = 'workload';
END;

CREATE TRIGGER trg_grants_no_update
BEFORE UPDATE ON grants
BEGIN
    SELECT RAISE(ABORT, 'immutable_grant');
END;

CREATE TRIGGER trg_grants_no_delete
BEFORE DELETE ON grants
BEGIN
    SELECT RAISE(ABORT, 'immutable_grant');
END;

CREATE TRIGGER trg_credential_revocations_no_update
BEFORE UPDATE ON credential_revocations
BEGIN
    SELECT RAISE(ABORT, 'immutable_credential_revocation');
END;

CREATE TRIGGER trg_credential_revocations_no_delete
BEFORE DELETE ON credential_revocations
BEGIN
    SELECT RAISE(ABORT, 'immutable_credential_revocation');
END;

CREATE TRIGGER trg_grant_revocations_no_update
BEFORE UPDATE ON grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'immutable_grant_revocation');
END;

CREATE TRIGGER trg_grant_revocations_no_delete
BEFORE DELETE ON grant_revocations
BEGIN
    SELECT RAISE(ABORT, 'immutable_grant_revocation');
END;
