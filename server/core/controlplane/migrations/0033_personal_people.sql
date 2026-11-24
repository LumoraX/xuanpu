-- 纯个人空间：人物档案、轻量关系边、原始互动留痕与幂等回执。
CREATE TABLE "${schema}"."${prefix}personal_person" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    person_id TEXT NOT NULL,
    name TEXT NOT NULL,
    organization TEXT NOT NULL DEFAULT '',
    birth_year INTEGER,
    birth_month INTEGER,
    birth_day INTEGER,
    profile JSONB NOT NULL,
    avatar JSONB,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    merged_into_id TEXT,
    merge_snapshot JSONB,
    trashed_at DOUBLE PRECISION,
    trash_batch TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, user_id, person_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id, merged_into_id)
        REFERENCES "${schema}"."${prefix}personal_person" (tenant_id, user_id, person_id),
    CHECK (merged_into_id IS NULL OR merged_into_id <> person_id)
);

CREATE INDEX "${prefix}personal_person_active_idx"
    ON "${schema}"."${prefix}personal_person" (tenant_id, user_id, updated_at DESC)
    WHERE trashed_at IS NULL AND merged_into_id IS NULL;

CREATE INDEX "${prefix}personal_person_birth_idx"
    ON "${schema}"."${prefix}personal_person" (tenant_id, user_id, birth_month, birth_day)
    WHERE trashed_at IS NULL AND merged_into_id IS NULL AND birth_month IS NOT NULL;

CREATE TABLE "${schema}"."${prefix}personal_relation" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    relation_id TEXT NOT NULL,
    from_person_id TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('person', 'matter')),
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    is_symmetric BOOLEAN NOT NULL DEFAULT FALSE,
    role TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    valid_from TEXT,
    valid_to TEXT,
    source_note_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    trashed_at DOUBLE PRECISION,
    trash_batch TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, user_id, relation_id),
    FOREIGN KEY (tenant_id, user_id, from_person_id)
        REFERENCES "${schema}"."${prefix}personal_person" (tenant_id, user_id, person_id),
    CHECK (NOT (target_kind = 'person' AND from_person_id = target_id)),
    CHECK (NOT (target_kind = 'matter' AND is_symmetric = TRUE))
);

CREATE INDEX "${prefix}personal_relation_from_idx"
    ON "${schema}"."${prefix}personal_relation" (tenant_id, user_id, from_person_id)
    WHERE trashed_at IS NULL;

CREATE INDEX "${prefix}personal_relation_target_idx"
    ON "${schema}"."${prefix}personal_relation" (tenant_id, user_id, target_kind, target_id)
    WHERE trashed_at IS NULL;

CREATE TABLE "${schema}"."${prefix}personal_note" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    note_id TEXT NOT NULL,
    occurred_at DOUBLE PRECISION NOT NULL,
    content TEXT NOT NULL,
    person_ids JSONB NOT NULL,
    matter_id TEXT,
    attachment_file_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_message_id BIGINT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    trashed_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, user_id, note_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

CREATE INDEX "${prefix}personal_note_time_idx"
    ON "${schema}"."${prefix}personal_note" (tenant_id, user_id, occurred_at DESC)
    WHERE trashed_at IS NULL;

CREATE TABLE "${schema}"."${prefix}personal_people_request" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY (tenant_id, user_id, request_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

-- 主对话待确认建议索引：覆盖人物复合确认卡 people_draft。
CREATE INDEX "${prefix}workspace_pending_people_proposal_idx"
    ON "${schema}"."${prefix}workspace_message" (tenant_id, user_id, message_id DESC)
    WHERE record->>'role' = 'assistant'
      AND right(request_id, 10) = ':assistant'
      AND jsonb_typeof(record#>'{metadata,route,people_draft}') = 'object';
