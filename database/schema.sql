-- ============================================================
-- Smart Parking Booking System — Supabase schema
-- Run this once in Supabase → SQL Editor → New Query → Run
-- ============================================================

create extension if not exists pgcrypto;

-- One row per physical parking slot
create table if not exists slots (
  id serial primary key,
  slot_number int unique not null,
  status text not null default 'free',
  -- status values: free | reserved | checkin_warning | occupied | exit_warning | overstay
  updated_at timestamptz not null default now()
);

-- One row per booking attempt for a slot
create table if not exists bookings (
  id uuid primary key default gen_random_uuid(),
  slot_id int references slots(id) not null,
  car_number text not null,
  duration_minutes int not null default 60,
  booked_at timestamptz not null default now(),
  expected_end_at timestamptz not null,
  check_in_time timestamptz,
  check_out_time timestamptz,
  status text not null default 'pending',
  -- status values:
  -- pending          -> booked, car hasn't arrived yet
  -- checkin_warning  -> 5+ min late arriving
  -- checked_in       -> car detected in the correct slot
  -- exit_warning     -> 5-10 min late leaving after expected_end_at
  -- overstay         -> 15+ min late leaving, extra_charge accruing
  -- cancelled        -> 10+ min late arriving, auto-cancelled
  -- completed        -> car left, booking closed
  extra_charge numeric not null default 0,
  updated_at timestamptz not null default now()
);

-- Audit trail / activity feed
create table if not exists booking_events (
  id bigserial primary key,
  booking_id uuid references bookings(id) on delete cascade,
  event_type text not null,
  message text,
  created_at timestamptz not null default now()
);

-- keep updated_at fresh
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_slots_updated on slots;
create trigger trg_slots_updated before update on slots
for each row execute function set_updated_at();

drop trigger if exists trg_bookings_updated on bookings;
create trigger trg_bookings_updated before update on bookings
for each row execute function set_updated_at();

-- ============================================================
-- Row Level Security
-- Demo-permissive policies so the frontend (anon key) can read/write
-- directly with no auth. Fine for a hackathon demo. Before any real
-- deployment, replace these with policies scoped to authenticated users.
-- ============================================================
alter table slots enable row level security;
alter table bookings enable row level security;
alter table booking_events enable row level security;

create policy "public read slots" on slots for select using (true);
create policy "public update slots" on slots for update using (true);
create policy "public insert slots" on slots for insert with check (true);

create policy "public read bookings" on bookings for select using (true);
create policy "public insert bookings" on bookings for insert with check (true);
create policy "public update bookings" on bookings for update using (true);

create policy "public read events" on booking_events for select using (true);
create policy "public insert events" on booking_events for insert with check (true);

-- ============================================================
-- Realtime replication
-- Without this, RLS-permitted rows can still be read/written fine, but the
-- frontend's `.on("postgres_changes", ...)` subscriptions never fire, which
-- is why the grid/timers/warnings only ever update on a manual page refresh.
-- Run this once. If it errors "already a member", it's already enabled —
-- check Database → Replication in the Supabase dashboard instead.
-- ============================================================
alter publication supabase_realtime add table slots;
alter publication supabase_realtime add table bookings;
alter publication supabase_realtime add table booking_events;