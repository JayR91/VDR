(function () {
  const BTN_CLASS = "vdr-overlay-btn";
  const IDLE_LABEL = "⬇ VDR";

  // Resolve the URL for the specific post a <video> belongs to, not just
  // "the current page" -- X/Twitter (and similar feeds) can show many
  // videos on one page, each from a different post. Walk up to the
  // enclosing <article> (the standard wrapper for one feed item) and use
  // its permalink (the /status/... link). Falls back to the page URL for
  // single-video pages (YouTube, Vimeo, a single Reddit/X post, ...).
  function findPostUrl(video) {
    // Element.closest() walks all the way to the document root with no
    // depth limit -- needed here since X nests a tweet's <video> as much
    // as 20+ DOM levels below its <article> wrapper.
    const article = video.closest("article");
    if (article) {
      const link =
        article.querySelector('a[href*="/status/"]') ||
        article.querySelector("a[href] time")?.closest("a");
      if (link && link.href) return link.href;
    }
    return location.href;
  }

  function sendMsg(type, extra) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(Object.assign({ type }, extra), (resp) => {
          void chrome.runtime.lastError;
          resolve(resp || {});
        });
      } catch (e) {
        resolve({});
      }
    });
  }

  // A real <a href="vdr://..."> click in the page's own DOM, not
  // chrome.tabs.create from the background script: Chrome will silently
  // let tabs.create "succeed" (valid tab, no error) without actually
  // handing off to the OS protocol handler when it's driven through the
  // extension APIs like that. A genuine anchor click is the same
  // mechanism real "Open in App" links on websites use, and is what
  // Chrome's external-protocol handling actually honors reliably. The
  // first time, the browser shows a one-time "Open in VDR?"
  // confirmation -- expected, and only needed once.
  function launchAppViaLink() {
    const a = document.createElement("a");
    a.href = "vdr://launch";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 100);
  }

  // Tries the download; if the app isn't running, launches it and polls
  // until it responds, then retries once. This whole multi-second sequence
  // runs here in the content script -- it stays alive as long as the page
  // does, unlike the background script's MV3 service worker, which Chrome
  // can suspend mid-wait.
  async function downloadViaApp(url) {
    let resp = await sendMsg("vdr_try_add", { url });
    if (resp.ok) return true;

    launchAppViaLink();
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 500));
      if ((await sendMsg("vdr_ping")).ok) break;
    }
    resp = await sendMsg("vdr_try_add", { url });
    return !!resp.ok;
  }

  function makeButton(video) {
    const btn = document.createElement("div");
    btn.className = BTN_CLASS;
    btn.textContent = IDLE_LABEL;
    Object.assign(btn.style, {
      position: "absolute",
      top: "8px",
      left: "8px",
      zIndex: 2147483647,
      background: "rgba(20,20,20,0.85)",
      color: "#fff",
      fontFamily: "Arial, Helvetica, sans-serif",
      fontWeight: "bold",
      fontSize: "12px",
      padding: "5px 10px",
      borderRadius: "6px",
      cursor: "pointer",
      userSelect: "none",
      boxShadow: "0 2px 6px rgba(0,0,0,0.4)",
      transition: "background 0.15s",
    });
    btn.addEventListener("mouseenter", () => {
      btn.style.background = "rgba(200,30,30,0.9)";
    });
    btn.addEventListener("mouseleave", () => {
      btn.style.background = "rgba(20,20,20,0.85)";
    });

    // The ONLY thing that ever triggers a send is a direct click on this
    // button. Attaching the overlay (including as videos scroll in/out of
    // view) never downloads anything by itself.
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      const url = findPostUrl(video);
      btn.textContent = "Sending…";
      const ok = await downloadViaApp(url);
      btn.textContent = ok ? "✓ Sent to VDR" : "✗ App not running";
      setTimeout(() => (btn.textContent = IDLE_LABEL), 2200);
    });
    ["mousedown", "mouseup"].forEach((evt) =>
      btn.addEventListener(evt, (e) => e.stopPropagation())
    );
    return btn;
  }

  function latchTarget(video) {
    // YouTube: the inner .html5-video-container sits *under* .ytp-chrome-top,
    // so a button parented there is in the DOM but invisible. Latch onto the
    // player root (.html5-video-player) which owns both layers. Everywhere
    // else, the <video>'s parent is the overlay host.
    return video.closest(".html5-video-player") || video.parentElement;
  }

  function hasLatch(container) {
    for (const child of container.children) {
      if (child.classList && child.classList.contains(BTN_CLASS)) return true;
    }
    return false;
  }

  function attach(video) {
    const container = latchTarget(video);
    if (!container) return;
    if (hasLatch(container)) return;
    if (getComputedStyle(container).position === "static") {
      container.style.position = "relative";
    }
    container.appendChild(makeButton(video));
  }

  function scan() {
    document.querySelectorAll("video").forEach(attach);
  }

  // Debounce bursts of DOM mutations (X's feed churns heavily while
  // scrolling) into at most one scan per animation frame.
  let scheduled = false;
  function scheduleScan() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      scan();
    });
  }

  new MutationObserver(scheduleScan).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  document.addEventListener("yt-navigate-finish", scheduleScan);

  scan();
  // Safety-net poll in case a site adds/replaces <video> elements without
  // triggering a childList mutation the observer catches.
  setInterval(scan, 1500);
})();
