-- 纯个人空间知识图谱升级：新增公司/组织一等实体、泛化跨实体关系边、升格原始记录支持会议纪要与行动项溯源。
CREATE TABLE "${schema}"."${prefix}personal_org" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    org_id TEXT NOT NULL,
    name TEXT NOT NULL,
    profile JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    merged_into_id TEXT,
    merge_snapshot JSONB,
    trashed_at DOUBLE PRECISION,
    trash_batch TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, user_id, org_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id, merged_into_id)
        REFERENCES "${schema}"."${prefix}personal_org" (tenant_id, user_id, org_id),
    CHECK (merged_into_id IS NULL OR merged_into_id <> org_id)
);

CREATE INDEX "${prefix}personal_org_active_idx"
    ON "${schema}"."${prefix}personal_org" (tenant_id, user_id, updated_at DESC)
    WHERE trashed_at IS NULL AND merged_into_id IS NULL;

-- 泛化关系边：允许 from_kind 与 target_kind 均覆盖 person / org / matter（保留旧列名 from_person_id 兼容已有数据）。
ALTER TABLE "${schema}"."${prefix}personal_relation"
    ADD COLUMN from_kind TEXT NOT NULL DEFAULT 'person';

DO $$$$
DECLARE
    c RECORD;
BEGIN
    FOR c IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = '"${schema}"."${prefix}personal_relation"'::regclass
          AND contype IN ('c', 'f')
    LOOP
        EXECUTE format(
            'ALTER TABLE "${schema}"."${prefix}personal_relation" DROP CONSTRAINT %I',
            c.conname
        );
    END LOOP;
END $$$$;

ALTER TABLE "${schema}"."${prefix}personal_relation"
    ADD FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id),
    ADD CHECK (version > 0),
    ADD CHECK (from_kind IN ('person', 'org', 'matter')),
    ADD CHECK (target_kind IN ('person', 'org', 'matter')),
    ADD CHECK (NOT (from_kind = target_kind AND from_person_id = target_id)),
    ADD CHECK (NOT ((from_kind = 'matter' OR target_kind = 'matter') AND is_symmetric = TRUE));

CREATE INDEX "${prefix}personal_relation_from_kind_idx"
    ON "${schema}"."${prefix}personal_relation" (tenant_id, user_id, from_kind, from_person_id)
    WHERE trashed_at IS NULL;

-- 升格原始记录表：支持随手记/会议纪要/名片/通话、标题、地点、结论要点与多类实体/行动项溯源。
ALTER TABLE "${schema}"."${prefix}personal_note"
    ADD COLUMN record_kind TEXT NOT NULL DEFAULT 'note'
        CHECK (record_kind IN ('note', 'meeting', 'card', 'call', 'other')),
    ADD COLUMN title TEXT NOT NULL DEFAULT '',
    ADD COLUMN location TEXT NOT NULL DEFAULT '',
    ADD COLUMN end_at DOUBLE PRECISION,
    ADD COLUMN summary TEXT NOT NULL DEFAULT '',
    ADD COLUMN decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN open_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN org_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN matter_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN todo_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN reminder_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX "${prefix}personal_note_kind_time_idx"
    ON "${schema}"."${prefix}personal_note" (tenant_id, user_id, record_kind, occurred_at DESC)
    WHERE trashed_at IS NULL;
