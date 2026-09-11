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
ALTER TABLE chats ADD COLUMN IF NOT EXISTS membership_status TEXT NOT NULL DEFAULT 'joined';
ALTER TABLE chats ADD COLUMN IF NOT EXISTS disposition TEXT NOT NULL DEFAULT 'active';
ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_history_scanned TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS group_repost_state (
  chat_id BIGINT PRIMARY KEY,
  template_text TEXT NOT NULL,
  current_message_id BIGINT NOT NULL,
  inbound_count INTEGER NOT NULL DEFAULT 0,
  template_version INTEGER NOT NULL DEFAULT 1,
  repost_pending BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS link_targets (
  id BIGSERIAL PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  source_chat_id BIGINT,
  target_chat_id BIGINT,
  title TEXT,
  kind TEXT,
  access_status TEXT NOT NULL DEFAULT 'not_joined'
    CHECK(access_status IN ('joined','not_joined','invalid','inaccessible')),
  disposition TEXT NOT NULL DEFAULT 'active'
    CHECK(disposition IN ('active','discarded')),
  last_error TEXT,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_checked TIMESTAMPTZ
);
ALTER TABLE link_targets ADD COLUMN IF NOT EXISTS request_needed BOOLEAN NOT NULL DEFAULT FALSE;
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
INSERT INTO link_targets(url,source_chat_id,first_seen)
SELECT DISTINCT ON (url) url,chat_id,observed_at FROM discovered_links
WHERE url ~* '^(https?://)?(t\\.me|telegram\\.me)/'
ORDER BY url,observed_at
ON CONFLICT(url) DO NOTHING;
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

-- Optional, independent post-link branch.  It does not alter the primary PV
-- cadence and stores classifications only, never incoming message text.
CREATE TABLE IF NOT EXISTS pv_two_screens_sessions (
  user_id BIGINT PRIMARY KEY REFERENCES pv_reply_contacts(user_id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN (
    'prompt_queued','awaiting_optin','question_queued','limit_queued',
    'followup_queued','awaiting_choice','photo_queued','completed','stopped'
  )),
  selected_slot TEXT CHECK(selected_slot IN ('peitos','buceta','cu')),
  choice_retry_sent BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);
ALTER TABLE pv_two_screens_sessions
ADD COLUMN IF NOT EXISTS choice_retry_sent BOOLEAN NOT NULL DEFAULT FALSE;

-- The control-bot chat is the operator-managed source of the current photo.
-- Keeping a Telegram message reference permits replacing a slot without
-- persisting files or explicit media in the database/repository.
CREATE TABLE IF NOT EXISTS pv_two_screens_media_slots (
  slot TEXT PRIMARY KEY CHECK(slot IN ('peitos','buceta','cu')),
  source_peer BIGINT NOT NULL,
  source_message_id BIGINT NOT NULL,
  updated_by BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Consent and per-event delivery state for administrator-triggered live alerts.
CREATE TABLE IF NOT EXISTS live_alert_subscriptions (
  user_id BIGINT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('pending','subscribed','declined','unsubscribed')),
  asked_at TIMESTAMPTZ,
  responded_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS live_campaigns (
  id BIGSERIAL PRIMARY KEY,
  link TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'sending'
    CHECK(status IN ('sending','sent','cancelled')),
  created_by BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS live_campaign_recipients (
  campaign_id BIGINT NOT NULL REFERENCES live_campaigns(id),
  user_id BIGINT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'queued'
    CHECK(stage IN ('queued','awaiting','remarketing_sent','link_queued','delivered','stopped')),
  invited_at TIMESTAMPTZ,
  remarketing_at TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(campaign_id,user_id)
);
CREATE INDEX IF NOT EXISTS live_campaign_recipient_lookup_idx
ON live_campaign_recipients(user_id,stage,campaign_id DESC);

-- One-time backfill for conversations already inside the active PV sequence.
INSERT INTO outbox_actions(action_key,module_id,action_type,payload,available_at)
SELECT 'pv_reply:live-optin:' || c.user_id,
       'pv_reply','send_live_optin',
       jsonb_build_object('peer',c.user_id,'campaign_id','pv.live_optin'),
       GREATEST(NOW(),c.link_sent_at + INTERVAL '20 minutes')
FROM pv_reply_contacts c
LEFT JOIN live_alert_subscriptions s ON s.user_id=c.user_id
WHERE c.stage IN ('following_up','weekly')
  AND c.link_sent_at IS NOT NULL
  AND s.user_id IS NULL
ON CONFLICT(action_key) DO NOTHING;

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
