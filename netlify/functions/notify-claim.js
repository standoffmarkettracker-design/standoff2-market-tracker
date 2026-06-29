// notify-claim.js
// Called by the site when a user submits a gold allowance claim
// Sends a Discord notification to the admin with claim details

const DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1521212880614330488/whaIWiT5y7FbVYQ93k0TwHtbPljg5CxdG8vwr8J9s6vbWTturZluUyKhHTNKcL0Ytzj5";
const FIREBASE_URL    = "https://standoff-2-tracker-default-rtdb.firebaseio.com";

exports.handler = async function(event) {
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method not allowed" };
  }

  let claim;
  try {
    claim = JSON.parse(event.body);
  } catch(e) {
    return { statusCode: 400, body: "Invalid JSON" };
  }

  const { uid, username, itemName, listPrice, allowance, tier, displayName } = claim;

  if (!uid || !username || !itemName || !listPrice) {
    return { statusCode: 400, body: "Missing required fields" };
  }

  // Send Discord notification
  const message = {
    embeds: [{
      title: "💰 New Gold Allowance Claim",
      color: tier === "VIP+" ? 0xFFD700 : tier === "VIP" ? 0xCE93D8 : 0x4FC3F7,
      fields: [
        { name: "User", value: displayName || uid, inline: true },
        { name: "Tier", value: tier, inline: true },
        { name: "Allowance", value: `${allowance} G`, inline: true },
        { name: "Seller Username", value: `\`${username}\``, inline: true },
        { name: "Item to Buy", value: `\`${itemName}\``, inline: true },
        { name: "List Price", value: `\`${listPrice} G\``, inline: true },
      ],
      description: `**Action required:** Go to the Standoff 2 marketplace and buy **${itemName}** listed by **${username}** at exactly **${listPrice} G**`,
      footer: { text: "After buying, click Mark Complete on the site" },
      timestamp: new Date().toISOString(),
    }]
  };

  try {
    const resp = await fetch(DISCORD_WEBHOOK, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message),
    });

    if (!resp.ok) {
      const err = await resp.text();
      console.error("Discord webhook error:", err);
      return { statusCode: 500, body: "Discord notification failed" };
    }

    return {
      statusCode: 200,
      body: JSON.stringify({ ok: true }),
    };
  } catch(e) {
    console.error("Notify error:", e);
    return { statusCode: 500, body: e.message };
  }
};
