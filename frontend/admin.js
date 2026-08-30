import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

const tbody = document.getElementById("admin-tbody");
const statsEl = document.getElementById("admin-stats");
const resetAllBtn = document.getElementById("reset-all-btn");

const ACTIVE_STATUSES = ["pending", "checkin_warning", "checked_in", "exit_warning", "overstay"];

const STATUS_LABEL = {
  free: "Free",
  pending: "Reserved — awaiting arrival",
  checkin_warning: "Late arriving",
  checked_in: "Occupied",
  exit_warning: "Time's up",
  overstay: "Overstay",
  // Set on the slot row itself (not a booking) when a car is physically
  // parked with no reservation behind it -- must still read as occupied.
  occupied: "Occupied (no booking)",
};

// Same rule as the customer site: a booking's status wins when one exists;
// otherwise fall back to the slot row's own status, since the detector can
// mark a slot occupied with no booking at all (walk-in / already-parked car).
function statusForSlot(slot, booking) {
  if (booking) return booking.status;
  if (slot.status && slot.status !== "free") return slot.status;
  return "free";
}

let slots = [];
let bookings = [];

function fmtTime(ts) {
  return ts ? new Date(ts).toLocaleTimeString() : "—";
}

async function loadData() {
  const [{ data: slotData, error: slotErr }, { data: bookingData }] = await Promise.all([
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
}

function bookingForSlot(slotId) {
  return bookings.find((b) => b.slot_id === slotId);
}

async function cancelBooking(booking) {
  if (!confirm(`Cancel the booking for ${booking.car_number} and free the slot?`)) return;
  await supabase.from("bookings").update({ status: "cancelled" }).eq("id", booking.id);
  await supabase.from("slots").update({ status: "free" }).eq("id", booking.slot_id);
  loadData();
}

async function forceCheckout(booking) {
  if (!confirm(`Force checkout for ${booking.car_number}? Marks the booking completed and frees the slot.`)) return;
  await supabase.from("bookings").update({
    status: "completed",
    check_out_time: new Date().toISOString(),
  }).eq("id", booking.id);
  await supabase.from("slots").update({ status: "free" }).eq("id", booking.slot_id);
  loadData();
}

resetAllBtn.addEventListener("click", async () => {
  if (!confirm("Reset ALL slots? This cancels every active booking and frees every slot — only use this for demo resets.")) return;

  const activeIds = bookings.map((b) => b.id);
  if (activeIds.length > 0) {
    await supabase.from("bookings").update({ status: "cancelled" }).in("id", activeIds);
  }
  const slotIds = slots.map((s) => s.id);
  if (slotIds.length > 0) {
    await supabase.from("slots").update({ status: "free" }).in("id", slotIds);
  }
  loadData();
});

function render() {
  tbody.innerHTML = "";
  let free = 0;

  slots.forEach((slot) => {
    const booking = bookingForSlot(slot.id);
    const status = statusForSlot(slot, booking);
    if (status === "free") free++;

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>P${slot.slot_number}</td>
      <td><span class="badge badge-${status}">${STATUS_LABEL[status] || status}</span></td>
      <td>${booking ? booking.car_number : "—"}</td>
      <td>${booking ? fmtTime(booking.booked_at) : "—"}</td>
      <td>${booking ? fmtTime(booking.expected_end_at) : "—"}</td>
      <td>${booking ? fmtTime(booking.check_in_time) : "—"}</td>
      <td>${booking && booking.extra_charge > 0 ? `₹${booking.extra_charge}` : "—"}</td>
      <td class="actions-cell"></td>
    `;

    const actionsCell = tr.querySelector(".actions-cell");
    if (booking && ["pending", "checkin_warning"].includes(booking.status)) {
      const btn = document.createElement("button");
      btn.textContent = "Cancel";
      btn.className = "admin-btn danger";
      btn.addEventListener("click", () => cancelBooking(booking));
      actionsCell.appendChild(btn);
    } else if (booking && ["checked_in", "exit_warning", "overstay"].includes(booking.status)) {
      const btn = document.createElement("button");
      btn.textContent = "Force checkout";
      btn.className = "admin-btn warn";
      btn.addEventListener("click", () => forceCheckout(booking));
      actionsCell.appendChild(btn);
    } else {
      actionsCell.textContent = "—";
    }

    tbody.appendChild(tr);
  });

  statsEl.textContent = `${free} free · ${slots.length - free} in use / ${slots.length} total`;
}

supabase
  .channel("admin:slots")
  .on("postgres_changes", { event: "*", schema: "public", table: "slots" }, loadData)
  .subscribe();

supabase
  .channel("admin:bookings")
  .on("postgres_changes", { event: "*", schema: "public", table: "bookings" }, loadData)
  .subscribe();

loadData();