-- ClinicCall schema. Fictional demo data only.
-- Safe to run more than once.

create table if not exists doctors (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  display_name text not null,
  specialty text not null,
  active boolean not null default true
);

create table if not exists slots (
  id uuid primary key default gen_random_uuid(),
  doctor_id uuid not null references doctors(id),
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  unique (doctor_id, starts_at),
  check (ends_at > starts_at)
);
create index if not exists slots_starts_at_idx on slots (starts_at);

create table if not exists appointments (
  id uuid primary key default gen_random_uuid(),
  booking_reference text not null unique
    check (booking_reference ~ '^CL-[0-9]{6}$'),
  slot_id uuid not null references slots(id),
  patient_name text not null check (length(trim(patient_name)) between 1 and 40),
  status text not null default 'confirmed' check (status in ('confirmed','cancelled')),
  idempotency_key text unique,
  version int not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  cancelled_at timestamptz,
  check ((status = 'cancelled') = (cancelled_at is not null))
);

-- The final guard against double-booking: one confirmed appointment per slot.
create unique index if not exists one_confirmed_appointment_per_slot
  on appointments (slot_id) where status = 'confirmed';
create index if not exists appointments_status_idx on appointments (status);

create table if not exists conversation_sessions (
  id uuid primary key default gen_random_uuid(),
  state text not null check (state in
    ('IDLE','COLLECTING','AWAITING_CONFIRMATION','DISAMBIGUATING','COMPLETED')),
  flow text,
  context jsonb not null default '{}',
  turn_no int not null default 0,
  version int not null default 0,
  last_turn_id uuid,
  last_response jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists conversation_events (
  id bigint generated always as identity primary key,
  session_id uuid not null references conversation_sessions(id) on delete cascade,
  turn_no int not null,
  seq int not null,
  kind text not null,
  payload jsonb not null default '{}',
  executed boolean,
  created_at timestamptz not null default now(),
  unique (session_id, turn_no, seq)
);

-- RLS on with no policies: the anon/authenticated API keys can read nothing.
-- The backend connects with the direct database credentials instead.
alter table doctors enable row level security;
alter table slots enable row level security;
alter table appointments enable row level security;
alter table conversation_sessions enable row level security;
alter table conversation_events enable row level security;