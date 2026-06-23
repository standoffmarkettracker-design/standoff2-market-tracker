/**
 * Ko-fi Webhook Handler
 * 
 * Required Netlify environment variables:
 *   KOFI_VERIFICATION_TOKEN  — from Ko-fi Settings -> API
 *   FIREBASE_DB_SECRET       — from Firebase Console -> Project Settings -> Service Accounts -> Database Secrets
 *
 * Ko-fi webhook URL: https://standoff2markettracker.com/.netlify/functions/kofi-webhook
 */

const FIREBASE_DB = "https://standoff-2-tracker-default-rtdb.firebaseio.com";

function tierFromAmount(amount) {
  if (amount >= 25) return "VIP+";
  if (amount >= 10) return "VIP";
  if (amount >= 1)  return "Basic";
  return null;
}

async function firebaseSet(path, value, dbSecret) {
  const url = FIREBASE_DB + "/" + path + ".json?auth=" + dbSecret;
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  });
  if (!res.ok) throw new Error("Firebase write failed: " + res.status + " " + await res.text());
  return res.json();
}

exports.handler = async function (event) {
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method Not Allowed" };
  }

  try {
    const params = new URLSearchParams(event.body);
    const rawData = params.get("data");
    if (!rawData) return { statusCode: 400, body: "No data field" };

    const data = JSON.parse(rawData);
    console.log("Ko-fi webhook received:", JSON.stringify(data));

    // Verify the Ko-fi token
    const kofiToken = process.env.KOFI_VERIFICATION_TOKEN;
    if (kofiToken && data.verification_token !== kofiToken) {
      console.warn("Invalid Ko-fi verification token");
      return { statusCode: 401, body: "Unauthorized" };
    }

    // Extract Firebase UID from the message field (Firebase UIDs are 28 alphanumeric chars)
    const message = (data.message || "").trim();
    const uidMatch = message.match(/\b([A-Za-z0-9]{20,})\b/);
    if (!uidMatch) {
      console.warn("No valid UID found in message:", message);
      return { statusCode: 200, body: "OK - no UID in message, tier not activated" };
    }
    const uid = uidMatch[1];

    // Determine tier from payment amount
    const amount = parseFloat(data.amount || 0);
    const tier   = tierFromAmount(amount);
    if (!tier) {
      console.warn("Amount too small:", amount);
      return { statusCode: 200, body: "OK - amount too small" };
    }

    const dbSecret = process.env.FIREBASE_DB_SECRET;
    if (!dbSecret) throw new Error("FIREBASE_DB_SECRET env var not set");

    // Set tier and expiry (30 days from now)
    const expiry = new Date();
    expiry.setDate(expiry.getDate() + 30);

    await Promise.all([
      firebaseSet("users/" + uid + "/tier",             tier,                  dbSecret),
      firebaseSet("users/" + uid + "/tierExpiry",       expiry.toISOString(),  dbSecret),
      firebaseSet("users/" + uid + "/tierActivatedAt",  new Date().toISOString(), dbSecret),
      firebaseSet("users/" + uid + "/kofiEmail",        data.email || "",      dbSecret),
      firebaseSet("users/" + uid + "/kofiName",         data.from_name || "",  dbSecret),
    ]);

    // Log to Firebase audit trail
    const txId = data.kofi_transaction_id || Date.now().toString();
    await firebaseSet("tierActivations/" + txId, {
      uid, tier, amount,
      email: data.email || "",
      name:  data.from_name || "",
      message,
      activatedAt: new Date().toISOString(),
      isSubscription: !!data.is_subscription_payment,
    }, dbSecret);

    console.log("Activated " + tier + " for UID " + uid + " (" + data.from_name + ", $" + amount + ")");
    return { statusCode: 200, body: "OK - " + tier + " activated for " + uid };

  } catch (err) {
    console.error("Ko-fi webhook error:", err.message);
    return { statusCode: 500, body: "Internal error: " + err.message };
  }
};
