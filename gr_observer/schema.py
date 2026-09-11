"""PostgreSQL schema owned by the monolith."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_control (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  reason TEXT NOT NULL DEFAULT 'Pausado',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO observer_control(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS module_control (
  module_id TEXT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  reason TEXT NOT NULL DEFAULT 'Pausado',
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO module_control(module_id, enabled, reason)
SELECT 'radar', enabled, reason FROM observer_control WHERE singleton=TRUE
ON CONFLICT(module_id) DO NOTHING;
INSERT INTO module_control(module_id, enabled, reason)
VALUES('botson', FALSE, 'Desligado por padrão; requer homologação')
ON CONFLICT(module_id) DO NOTHING;
INSERT INTO module_control(module_id, enabled, reason)
VALUES('pv_reply', FALSE, 'Desligado por padrão; requer ativação consciente')
ON CONFLICT(module_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS chats (
  chat_id BIGINT PRIMARY KEY,
  title TEXT NOT NULL,
  username TEXT,
  kind TEXT NOT NULL,
  can_text BOOLEAN,
  can_media BOOLEAN,
  can_links BOOLEAN,
  slowmode_seconds INTEGER,
  risk TEXT NOT NULL DEFAULT 'unknown',
  last_scanned TIMESTAMPTZ,
  last_seen TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS visible_rules (
  chat_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  excerpt TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS observed_bots (
  chat_id BIGINT NOT NULL,
  bot_id BIGINT NOT NULL,
  username TEXT,
  display_name TEXT,
  evidence TEXT,
  last_seen TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(chat_id, bot_id)
);
CREATE TABLE IF NOT EXISTS discovered_links (
  chat_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  url TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY(chat_id, message_id, url)
);
CREATE TABLE IF NOT EXISTS group_interactions (
  user_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  interaction_type TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(user_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS private_origins (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  source_chat_id BIGINT,
  confidence TEXT NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id BIGSERIAL PRIMARY KEY,
  chat_id BIGINT,
  user_id BIGINT,
  reason TEXT NOT NULL,
  suggested_text TEXT NOT NULL,
  media_key TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL
);

-- Inbox deduplicates an inbound Telegram update before it can create work.
CREATE TABLE IF NOT EXISTS inbox_events (
  source TEXT NOT NULL,
  event_key TEXT NOT NULL,
  module_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(source, event_key)
);

-- Outbox is the only queue from persisted intent to an active Telegram action.
CREATE TABLE IF NOT EXISTS outbox_actions (
  id BIGSERIAL PRIMARY KEY,
  action_key TEXT NOT NULL UNIQUE,
  module_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','processing','succeeded','failed','review')),
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  lease_until TIMESTAMPTZ,
  result JSONB,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE outbox_actions
ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE INDEX IF NOT EXISTS outbox_pending_idx
ON outbox_actions(status, id);
CREATE INDEX IF NOT EXISTS outbox_ready_idx
ON outbox_actions(status, available_at, id);

-- No incoming private-message content is stored. This state is enough to
-- advance the two-message conversation and schedule bounded follow-ups.
CREATE TABLE IF NOT EXISTS pv_reply_contacts (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  display_name TEXT,
  stage TEXT NOT NULL DEFAULT 'new'
    CHECK(stage IN (
      'new','greeting_queued','awaiting_reply','link_queued',
      'following_up','weekly','completed','stopped'
    )),
  last_inbound_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_inbound_message_id BIGINT,
  greeting_queued_at TIMESTAMPTZ,
  greeting_sent_at TIMESTAMPTZ,
  link_queued_at TIMESTAMPTZ,
  link_sent_at TIMESTAMPTZ,
  followup_cycle INTEGER NOT NULL DEFAULT 0,
  weekly_cycle INTEGER NOT NULL DEFAULT 0,
  weekly_last_question_at TIMESTAMPTZ,
  next_followup_at TIMESTAMPTZ,
  stopped_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS module_runs (
  run_id TEXT PRIMARY KEY,
  module_id TEXT NOT NULL,
  trigger_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','review')),
  request JSONB NOT NULL,
  result JSONB,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS module_runs_latest_idx
ON module_runs(module_id, created_at DESC);

-- Every side effect receives a deterministic key. Interrupted effects are not
-- blindly repeated; they move to review unless a reconciler proves the result.
CREATE TABLE IF NOT EXISTS telegram_effects (
  effect_key TEXT PRIMARY KEY,
  outbox_action_id BIGINT REFERENCES outbox_actions(id),
  effect_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'processing'
    CHECK(status IN ('processing','succeeded','review')),
  attempts INTEGER NOT NULL DEFAULT 1,
  result JSONB,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
