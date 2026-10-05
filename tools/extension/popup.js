/* global GS_CONFIG, chrome */
const out = document.getElementById("out");
const go = document.getElementById("go");
document.getElementById("who").textContent = `Sending to ${new URL(GS_CONFIG.server).host} as "${GS_CONFIG.label}"`;

go.onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https:\/\/www\.amazon\.com\//.test(tab.url || "")) {
    await chrome.tabs.create({ url: "https://www.amazon.com/your-orders/orders" });
    out.textContent = "Opened your Amazon orders. Sign in if asked, then click the extension again.";
    return;
  }
  go.disabled = true;
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (server, invite) => { window.__GS_CFG__ = { url: server, token: invite, label: "invited" }; },
    args: [GS_CONFIG.server, GS_CONFIG.invite],
  });
  chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["scraper.js"] });
  out.textContent = "Running in your Amazon tab. It takes a few minutes. Keep that tab open; a message pops up there when it's done. You can close this.";
};
