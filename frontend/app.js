import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

const grid = document.getElementById("slot-grid");
const statsEl = document.getElementById("stats");
const eventList = document.getElementById("event-list");
const modal = document.getElementById("booking-modal");
const modalSlotNum = document.getElementById("modal-slot-num");
const carInput = document.getElementById("car-number-input");
const durationButtons = document.querySelectorAll(".duration-options button");
const confirmBtn = document.getElementById("confirm-booking");
const cancelBtn = document.getElementById("cancel-modal");
const myBookingBanner = document.getElementById("my-booking-banner");

let slots = [];
let bookings = [];
let selectedSlot = null;
let selectedDuration = 60;

const ACTIVE_STATUSES = ["pending", "checkin_warning", "checked_in", "exit_warning", "overstay"];
const MY_BOOKING_KEY = "myActiveBookingId";

// Mirrors OVERSTAY_CHARGE_START_MIN in backend/config.py — keep these in sync.
const OVERSTAY_CHARGE_START_SECONDS = 15 * 60;

const STATUS_LABEL = {
  free: "Free",
  pending: "Reserved — awaiting arrival",
  checkin_warning: "Late arriving!",
  checked_in: "Occupied",
  exit_warning: "Time's up — please exit",
  overstay: "Overstay — charging",
  // Set directly on the `slots` row by the detector when a car is sitting in
  // a slot with no active booking behind it (walk-in car, or one that was
  // already parked before the system started) — must stay blocked either way.
  occupied: "Occupied (no booking)",
};

// The ONLY source of truth for whether a slot can be booked. A booking's own
// status always wins when one exists; otherwise fall back to the slot row's
// own status, because the detector can mark a slot occupied even with no
// booking at all (an unbooked/already-parked car) — that must still block
// the slot instead of showing it as free.
function statusForSlot(slot, booking) {
  if (booking) return booking.status;
  if (slot.status && slot.status !== "free") return slot.status;
  return "free";
}

durationButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    durationButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedDuration = parseInt(btn.dataset.min, 10);
  });
});

cancelBtn.addEventListener("click", closeModal);

confirmBtn.addEventListener("click", async () => {
  const carNumber = carInput.value.trim().toUpperCase();
  if (!carNumber) {
    carInput.focus();
    return;
  }

  // Basic prank/duplicate-booking guard: block a second active booking from
  // the same browser. Anyone can still clear localStorage or use another
  // browser/device, so this is a courtesy check, not real auth — the
  // 10-minute no-show auto-cancel in booking_monitor.py is still the real
  // safety net against abandoned/prank bookings.
  const existingId = localStorage.getItem(MY_BOOKING_KEY);
  if (existingId) {
    const { data: existing } = await supabase.from("bookings").select("*").eq("id", existingId).single();
    if (existing && ACTIVE_STATUSES.includes(existing.status)) {
      alert("You already have an active booking. Finish or cancel it before booking another slot.");
      closeModal();
      renderMyBooking();
      return;
    }
    localStorage.removeItem(MY_BOOKING_KEY);
  }

  const bookedAt = new Date();
  const expectedEnd = new Date(bookedAt.getTime() + selectedDuration * 60000);

  confirmBtn.disabled = true;
  confirmBtn.textContent = "Booking…";

  const { data, error } = await supabase
    .from("bookings")
    .insert({
      slot_id: selectedSlot.id,
      car_number: carNumber,
      duration_minutes: selectedDuration,
      booked_at: bookedAt.toISOString(),
      expected_end_at: expectedEnd.toISOString(),
      status: "pending",
    })
    .select()
    .single();

  confirmBtn.disabled = false;
  confirmBtn.textContent = "Confirm Booking";

  if (error) {
    alert("Booking failed: " + error.message);
    return;
  }

  localStorage.setItem(MY_BOOKING_KEY, data.id);
  recordMyBooking(data.id, selectedSlot.slot_number, carNumber);
  await supabase.from("slots").update({ status: "pending" }).eq("id", selectedSlot.id);
  closeModal();
  renderMyBooking();
});

function openModal(slot) {
  selectedSlot = slot;
  modalSlotNum.textContent = `P${slot.slot_number}`;
  carInput.value = "";
  modal.classList.remove("hidden");
  carInput.focus();
}

function closeModal() {
  modal.classList.add("hidden");
  selectedSlot = null;
}

async function loadData() {
  const [{ data: slotData, error: slotErr }, { data: bookingData, error: bookErr }] = await Promise.all([
    supabase.from("slots").select("*").order("slot_number"),
    supabase.from("bookings").select("*").in("status", ACTIVE_STATUSES),
  ]);

  if (slotErr) {
    statsEl.textContent = "Could not load slots — check config.js";
    return;
  }

  slots = slotData || [];
  bookings = bookingData || [];
  render();
  renderMyBooking();
}

function bookingForSlot(slotId) {
  return bookings.find((b) => b.slot_id === slotId);
}

function render() {
  grid.innerHTML = "";
  let free = 0;

  slots.forEach((slot) => {
    const booking = bookingForSlot(slot.id);
    const status = statusForSlot(slot, booking);
    const carNumber = booking ? booking.car_number : null;
    if (status === "free") free++;

    const card = document.createElement("div");
    card.className = `slot-card ${status}`;
    card.innerHTML = `
      <div>
        <div class="slot-num">P${slot.slot_number}${carNumber ? ` · ${carNumber}` : ""}</div>
        <div class="slot-status">${STATUS_LABEL[status] || status}</div>
      </div>
      ${
        booking
          ? `<div>
               <div class="slot-timer" data-booking="${booking.id}"></div>
               ${booking.extra_charge > 0 ? `<div class="slot-charge">+₹${booking.extra_charge}</div>` : ""}
             </div>`
          : status === "free"
            ? `<div class="slot-status">Tap to book</div>`
            : `<div class="slot-status">Occupied — not bookable</div>`
      }
    `;

    // Only genuinely free slots are clickable — an occupied-but-unbooked
    // slot (walk-in car) must not be offered for booking.
    if (status === "free") {
      card.addEventListener("click", () => openModal(slot));
    }
    grid.appendChild(card);
  });

  statsEl.textContent = `${free} free · ${slots.length - free} in use / ${slots.length} total`;
  tickTimers();
}

// --- "Your booking" banner: tracks only what THIS browser booked, since
// there's no login system. Shown above the grid, separate from the shared
// activity feed (which is intentionally global — a staff/overview view).
async function renderMyBooking() {
  const id = localStorage.getItem(MY_BOOKING_KEY);
  if (!id) {
    myBookingBanner.classList.add("hidden");
    myBookingBanner.innerHTML = "";
    return;
  }

  const { data: booking } = await supabase.from("bookings").select("*").eq("id", id).single();
  if (!booking || !ACTIVE_STATUSES.includes(booking.status)) {
    // Booking wrapped up (completed or cancelled) — start this browser's
    // 2-hour clock on its own activity-feed entries for it.
    if (booking) markMyBookingFinished(booking.id);
    localStorage.removeItem(MY_BOOKING_KEY);
    myBookingBanner.classList.add("hidden");
    myBookingBanner.innerHTML = "";
    return;
  }

  const slot = slots.find((s) => s.id === booking.slot_id);
  const slotLabel = slot ? `P${slot.slot_number}` : "—";
  const canCancel = ["pending", "checkin_warning"].includes(booking.status);

  myBookingBanner.classList.remove("hidden");
  myBookingBanner.innerHTML = `
    <span>Your booking: <strong>${slotLabel}</strong> — ${booking.car_number} — ${STATUS_LABEL[booking.status] || booking.status}</span>
    ${canCancel ? `<button id="my-booking-cancel" class="banner-btn">Cancel</button>` : ""}
  `;

  if (canCancel) {
    document.getElementById("my-booking-cancel").addEventListener("click", async () => {
      if (!confirm("Cancel your booking?")) return;
      await supabase.from("bookings").update({ status: "cancelled" }).eq("id", booking.id);
      await supabase.from("slots").update({ status: "free" }).eq("id", booking.slot_id);
      localStorage.removeItem(MY_BOOKING_KEY);
      renderMyBooking();
    });
  }
}

function tickTimers() {
  const now = new Date();
  bookings.forEach((b) => {
    const el = document.querySelector(`[data-booking="${b.id}"]`);
    if (!el) return;

    if (["pending", "checkin_warning"].includes(b.status)) {
      const secs = Math.max(0, Math.floor((now - new Date(b.booked_at)) / 1000));
      el.textContent = `Waiting to arrive · ${formatSecs(secs)}`;
      return;
    }

    const diffSecs = Math.floor((now - new Date(b.expected_end_at)) / 1000);
    if (diffSecs <= 0) {
      el.textContent = `Time left · ${formatSecs(-diffSecs)}`;
    } else if (diffSecs < OVERSTAY_CHARGE_START_SECONDS) {
      const untilCharge = OVERSTAY_CHARGE_START_SECONDS - diffSecs;
      el.textContent = `Overtime · ${formatSecs(diffSecs)} (charge starts in ${formatSecs(untilCharge)})`;
    } else {
      el.textContent = `Overtime · ${formatSecs(diffSecs)} — ₹${b.extra_charge ?? 0} charged so far`;
    }
  });
}

function formatSecs(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// --- Live Activity is PRIVATE to this browser ---
// Every visitor was previously seeing everyone's booking events, which
// leaks other people's slot/car activity to strangers. This browser now
// only ever shows events tied to a booking IT made (tracked below), and
// still fades those out 2h after that booking finishes.
const MY_BOOKINGS_KEY = "myBookingsMeta"; // { [bookingId]: { slotNumber, carNumber, createdAt, finishedAt? } }
const HIDE_OWN_EVENTS_AFTER_MS = 2 * 60 * 60 * 1000; // 2 hours

function getMyBookingsMeta() {
  let meta;
  try {
    meta = JSON.parse(localStorage.getItem(MY_BOOKINGS_KEY) || "{}");
  } catch {
    meta = {};
  }
  // Drop entries that are already past their 2h fade — nothing left to show
  // for them, no reason to keep growing localStorage forever.
  const now = Date.now();
  let changed = false;
  for (const id of Object.keys(meta)) {
    if (meta[id].finishedAt && now - meta[id].finishedAt >= HIDE_OWN_EVENTS_AFTER_MS) {
      delete meta[id];
      changed = true;
    }
  }
  if (changed) localStorage.setItem(MY_BOOKINGS_KEY, JSON.stringify(meta));
  return meta;
}

function recordMyBooking(bookingId, slotNumber, carNumber) {
  const meta = getMyBookingsMeta();
  meta[bookingId] = { slotNumber, carNumber, createdAt: Date.now() };
  localStorage.setItem(MY_BOOKINGS_KEY, JSON.stringify(meta));
}

function markMyBookingFinished(bookingId) {
  const meta = getMyBookingsMeta();
  if (meta[bookingId]) {
    meta[bookingId].finishedAt = Date.now();
    localStorage.setItem(MY_BOOKINGS_KEY, JSON.stringify(meta));
  }
}

function isMyBookingEvent(bookingId) {
  if (!bookingId) return false; // system-level events (no booking) aren't "yours" to show
  return Object.prototype.hasOwnProperty.call(getMyBookingsMeta(), bookingId);
}

function isMyBookingExpired(bookingId) {
  const meta = getMyBookingsMeta()[bookingId];
  return !!(meta && meta.finishedAt && Date.now() - meta.finishedAt >= HIDE_OWN_EVENTS_AFTER_MS);
}

function addEventToLog(text, bookingId, createdAt) {
  if (!isMyBookingEvent(bookingId) || isMyBookingExpired(bookingId)) return;

  const meta = getMyBookingsMeta()[bookingId];
  const slotLabel = meta ? `P${meta.slotNumber}` : "";
  const timeLabel = new Date(createdAt || Date.now()).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit",
  });

  const li = document.createElement("li");
  li.textContent = slotLabel ? `${slotLabel} · ${timeLabel} — ${text}` : `${timeLabel} — ${text}`;
  li.dataset.booking = bookingId;
  eventList.prepend(li);
  while (eventList.children.length > 40) {
    eventList.removeChild(eventList.lastChild);
  }
}

// Sweep already-rendered entries every minute so ones that cross the 2-hour
// mark while the page stays open actually disappear, not just new ones.
function sweepExpiredMyEvents() {
  eventList.querySelectorAll("li[data-booking]").forEach((li) => {
    if (isMyBookingExpired(li.dataset.booking)) li.remove();
  });
}
setInterval(sweepExpiredMyEvents, 60 * 1000);

async function loadRecentEvents() {
  const myIds = Object.keys(getMyBookingsMeta());
  if (myIds.length === 0) return; // nothing of ours to show yet
  const { data } = await supabase
    .from("booking_events")
    .select("*")
    .in("booking_id", myIds)
    .order("created_at", { ascending: false })
    .limit(15);
  (data || []).reverse().forEach((e) => addEventToLog(e.message || e.event_type, e.booking_id, e.created_at));
}

supabase
  .channel("public:slots")
  .on("postgres_changes", { event: "*", schema: "public", table: "slots" }, loadData)
  .subscribe();

supabase
  .channel("public:bookings")
  .on("postgres_changes", { event: "*", schema: "public", table: "bookings" }, loadData)
  .subscribe();

supabase
  .channel("public:booking_events")
  .on("postgres_changes", { event: "INSERT", schema: "public", table: "booking_events" }, (payload) => {
    addEventToLog(payload.new.message || payload.new.event_type, payload.new.booking_id, payload.new.created_at);
  })
  .subscribe();

loadData();
loadRecentEvents();
setInterval(tickTimers, 1000);