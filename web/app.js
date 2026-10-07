const $ = (selector) => document.querySelector(selector);
const video = $("#video");
const profileSelect = $("#profile-select");
const playButton = $("#play-button");
const toast = $("#toast");
let state = null;
let settingsState = [];
let recordingState = null;
let recordingSettingsLoaded = false;
let toastTimer = null;
let previewZoom = 1;

function notify(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : {};
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `درخواست ناموفق بود (${response.status}).`);
  }
  return data;
}

function setConnection(connected) {
  $("#connection-dot").classList.toggle("online", connected);
  $("#connection-dot").classList.toggle("offline", !connected);
  $("#connection-label").textContent = connected ? "متصل" : "قطع";
}

function renderProfiles(profiles) {
  const previous = profileSelect.value;
  profileSelect.replaceChildren();
  profiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.token;
    option.textContent = `${profile.width}×${profile.height} · ${profile.fps} fps`;
    option.disabled = !profile.uri;
    profileSelect.append(option);
  });
  if (profiles.some((profile) => profile.token === previous && profile.uri)) {
    profileSelect.value = previous;
  }
  const selected = profiles.find((profile) => profile.token === profileSelect.value);
  profileSelect.disabled = !selected;
  playButton.disabled = !selected;
  $("#record-button").disabled = !selected && !recordingState?.recording;
  $("#snapshot-button").disabled = !selected;
  $("#stream-detail").textContent = selected
    ? `${selected.encoding} · ${selected.width}×${selected.height} · ${selected.fps} fps`
    : "استریم قابل‌دسترسی نیست";
  $("#profile-summary").replaceChildren();
  profiles.forEach((profile) => {
    const row = document.createElement("div");
    row.className = "profile-row";
    const name = document.createElement("span");
    name.textContent = profile.name || profile.token;
    const details = document.createElement("strong");
    details.textContent = `${profile.width}×${profile.height} · ${profile.fps} fps`;
    row.append(name, details);
    $("#profile-summary").append(row);
  });
}

function renderCapabilities(data) {
  const ptzSupported = data.profiles.some((profile) => profile.ptzToken);
  $("#ptz-badge").textContent = ptzSupported ? "فرمان کوتاه PTZ پذیرفته می‌شود" : "PTZ اعلام نشده";
  $("#ptz-badge").className = `feature-badge ${ptzSupported ? "available" : ""}`;
  document.querySelectorAll("[data-direction], #stop-button").forEach((button) => {
    button.disabled = !ptzSupported;
  });
  const relayCount = Number(data.features.relayOutputsAdvertised || 0);
  const relayAdvertised = relayCount > 0;
  $("#relay-badge").textContent = relayAdvertised ? "خروجی اعلام شده · کنترل را بیازمایید" : "خروجی اعلام نشده";
  $("#relay-badge").className = `feature-badge ${relayAdvertised ? "limited" : ""}`;
  $("#relay-on").disabled = !relayAdvertised;
  $("#relay-off").disabled = !relayAdvertised;

  const audioChannels = (data.audioChannels || []).reduce((sum, value) => sum + Number(value || 0), 0);
  const entries = [
    { label: "پخش زنده H.264", state: data.profiles.length > 0 },
    { label: "حرکت PTZ", state: ptzSupported, limited: true },
    { label: "خروجی رله", state: relayAdvertised, limited: relayAdvertised },
    { label: "صدای دوربین", state: audioChannels > 0 },
    { label: "تصویر لحظه‌ای", state: data.profiles.some((profile) => profile.uri) },
  ];
  const list = $("#capability-list");
  list.replaceChildren();
  entries.forEach((entry) => {
    const item = document.createElement("li");
    const dot = document.createElement("span");
    dot.className = `cap-dot ${entry.state ? (entry.limited ? "warn" : "good") : ""}`;
    const label = document.createElement("span");
    const status = entry.label.includes("PTZ")
      ? "فرمان کوتاه پذیرفته شد"
      : entry.limited ? "اعلام شده؛ نیازمند تأیید فرمان" : "آماده";
    label.textContent = `${entry.label} · ${entry.state ? status : "در دسترس نیست"}`;
    item.append(dot, label);
    list.append(item);
  });
}

function renderSettingsProfile() {
  const profile = settingsState.find((item) => item.profileToken === $("#settings-profile").value);
  const controls = [
    $("#settings-resolution"), $("#settings-fps"), $("#settings-quality"),
    $("#settings-gop"), $("#settings-save"),
  ];
  controls.forEach((control) => { control.disabled = !profile; });
  if (!profile) return;

  const resolutionSelect = $("#settings-resolution");
  resolutionSelect.replaceChildren();
  profile.resolutions.forEach(({ width, height }) => {
    const option = document.createElement("option");
    option.value = `${width}x${height}`;
    option.textContent = `${width} × ${height}`;
    resolutionSelect.append(option);
  });
  resolutionSelect.value = `${profile.resolution.width}x${profile.resolution.height}`;

  const frameRate = $("#settings-fps");
  frameRate.min = profile.ranges.frameRate.min;
  frameRate.max = profile.ranges.frameRate.max;
  frameRate.value = profile.frameRate;

  $("#settings-interval").textContent = profile.encodingInterval;

  const quality = $("#settings-quality");
  quality.min = profile.ranges.quality.min;
  quality.max = profile.ranges.quality.max;
  quality.value = profile.quality;
  $("#settings-quality-value").textContent = profile.quality;

  const gop = $("#settings-gop");
  gop.min = profile.ranges.gop.min;
  gop.max = profile.ranges.gop.max;
  gop.value = profile.gop;
  $("#settings-gop-value").textContent = profile.gop;
  $("#settings-bitrate").textContent = `${profile.bitrate} kbps`;
  $("#settings-codec").textContent = `${profile.encoding} · ${profile.h264Profile}`;
  $("#settings-message").className = "settings-message";
  $("#settings-message").textContent =
    `بازه‌ی مجاز نرخ فریم ${profile.ranges.frameRate.min} تا ${profile.ranges.frameRate.max}؛ کیفیت ${profile.ranges.quality.min} تا ${profile.ranges.quality.max}؛ GOP ${profile.ranges.gop.min} تا ${profile.ranges.gop.max}.`;
}

async function refreshSettings() {
  const message = $("#settings-message");
  $("#settings-save").disabled = true;
  $("#settings-profile").disabled = true;
  message.className = "settings-message";
  message.textContent = "در حال خواندن تنظیمات دوربین...";
  try {
    const data = await api("/api/settings");
    settingsState = data.profiles || [];
    const profileSelectElement = $("#settings-profile");
    const previous = profileSelectElement.value;
    profileSelectElement.replaceChildren();
    settingsState.forEach((profile) => {
      const option = document.createElement("option");
      option.value = profile.profileToken;
      option.textContent = `${profile.profileName} · ${profile.resolution.width}×${profile.resolution.height}`;
      profileSelectElement.append(option);
    });
    profileSelectElement.disabled = settingsState.length === 0;
    if (settingsState.some((profile) => profile.profileToken === previous)) {
      profileSelectElement.value = previous;
    }
    if (settingsState.length) {
      renderSettingsProfile();
    } else {
      message.textContent = "تنظیمات قابل‌ویرایش از دوربین دریافت نشد.";
    }
  } catch (error) {
    settingsState = [];
    message.className = "settings-message error";
    message.textContent = `خواندن تنظیمات ناموفق بود: ${error.message}`;
    notify(error.message, true);
  }
}

async function refreshStatus() {
  $("#refresh-button").disabled = true;
  try {
    const data = await api("/api/status");
    state = data;
    $("#camera-address").textContent = data.ip || "—";
    setConnection(data.connected);
    const device = data.device || {};
    $("#camera-title").textContent = device.model || "دوربین شبکه";
    $("#camera-subtitle").textContent = device.firmware
      ? `نسخه‌ی نرم‌افزار ${device.firmware}`
      : (data.errors || ["اطلاعات دستگاه در دسترس نیست."])[0];
    $("#info-maker").textContent = device.manufacturer || "—";
    $("#info-model").textContent = device.model || "—";
    $("#info-firmware").textContent = device.firmware || "—";
    $("#info-serial").textContent = device.serial || "—";
    $("#info-clock").textContent = data.cameraTime || "—";
    renderProfiles(data.profiles || []);
    renderCapabilities(data);
    $("#last-updated").textContent = `آخرین به‌روزرسانی ${new Date().toLocaleTimeString("fa-IR")}`;
    if (data.errors?.length) notify(data.errors[0], true);
  } catch (error) {
    setConnection(false);
    $("#camera-subtitle").textContent = "ارتباط با سرویس دوربین برقرار نشد.";
    notify(error.message, true);
  } finally {
    $("#refresh-button").disabled = false;
  }
}

function selectedProfile() {
  return profileSelect.value;
}

function updateRecordingUI(data) {
  recordingState = data;
  const button = $("#record-button");
  button.disabled = !data.recording && !selectedProfile();
  button.classList.toggle("recording", data.recording);
  button.textContent = data.recording ? "■ توقف و ذخیره" : "● شروع ضبط";
  const status = $("#record-status");
  status.classList.toggle("active", data.recording);
  status.textContent = data.recording
    ? `در حال ضبط · ${new Intl.NumberFormat("fa-IR").format(data.elapsedSeconds)} ثانیه`
    : data.error ? `خطای ضبط: ${data.error}` : "ضبط متوقف است";
  if (!recordingSettingsLoaded) {
    $("#record-folder").value = data.folder;
    $("#record-minutes").value = data.segmentMinutes;
    $("#record-settings-message").textContent = "تنظیمات ضبط در این رایانه ذخیره می‌شود.";
    recordingSettingsLoaded = true;
  }
}

async function refreshRecording() {
  try {
    updateRecordingUI(await api("/api/recording"));
  } catch (error) {
    $("#record-status").textContent = `وضعیت ضبط دریافت نشد: ${error.message}`;
    $("#record-status").classList.add("active");
  }
}

function startVideo() {
  const token = selectedProfile();
  if (!token) return;
  $("#video-error").hidden = true;
  $("#video-placeholder").hidden = true;
  video.hidden = false;
  applyPreviewZoom();
  $("#live-badge").hidden = false;
  video.dataset.playing = "true";
  video.src = `/api/stream?profile=${encodeURIComponent(token)}&t=${Date.now()}`;
  $("#play-icon").textContent = "Ⅱ";
  $("#play-label").textContent = "توقف پخش";
}

function stopVideo() {
  video.dataset.playing = "false";
  video.removeAttribute("src");
  video.hidden = true;
  $("#live-badge").hidden = true;
  $("#video-placeholder").hidden = false;
  $("#play-icon").textContent = "▶";
  $("#play-label").textContent = "شروع پخش";
}

function applyPreviewZoom() {
  video.style.transform = `scale(${previewZoom})`;
  $("#zoom-level").textContent = `${new Intl.NumberFormat("fa-IR").format(previewZoom)}× · فقط پیش‌نمایش`;
}

function setPreviewZoom(next) {
  previewZoom = Math.min(4, Math.max(1, Math.round(next * 4) / 4));
  applyPreviewZoom();
}

$("#zoom-in").addEventListener("click", () => setPreviewZoom(previewZoom + 0.25));
$("#zoom-out").addEventListener("click", () => setPreviewZoom(previewZoom - 0.25));
$("#zoom-reset").addEventListener("click", () => setPreviewZoom(1));

video.addEventListener("error", () => {
  if (video.dataset.playing !== "true") return;
  video.dataset.playing = "false";
  video.hidden = true;
  $("#live-badge").hidden = true;
  $("#video-placeholder").hidden = true;
  $("#video-error").textContent = "پخش استریم شروع نشد؛ وضعیت اتصال دوربین و FFmpeg را بررسی کنید.";
  $("#video-error").hidden = false;
  $("#play-icon").textContent = "▶";
  $("#play-label").textContent = "تلاش دوباره";
});

profileSelect.addEventListener("change", () => {
  if (video.dataset.playing === "true") startVideo();
  const selected = state?.profiles.find((profile) => profile.token === profileSelect.value);
  $("#stream-detail").textContent = selected
    ? `${selected.encoding} · ${selected.width}×${selected.height} · ${selected.fps} fps`
    : "استریم قابل‌دسترسی نیست";
});

playButton.addEventListener("click", () => {
  if (video.dataset.playing === "true") stopVideo();
  else startVideo();
});

$("#refresh-button").addEventListener("click", refreshStatus);
$("#snapshot-button").addEventListener("click", () => {
  const token = selectedProfile();
  if (token) window.open(`/api/snapshot?profile=${encodeURIComponent(token)}`, "_blank", "noopener");
});

async function sendPtz(action, direction) {
  try {
    const result = await api("/api/ptz", {
      method: "POST",
      body: JSON.stringify({
        action,
        direction,
        profile: selectedProfile(),
        speed: Number($("#ptz-speed").value) / 100,
      }),
    });
    notify(result.message);
  } catch (error) {
    notify(error.message, true);
  }
}

document.querySelectorAll("[data-direction]").forEach((button) => {
  button.addEventListener("click", () => {
    if (!button.disabled) sendPtz("move", button.dataset.direction);
  });
});
$("#stop-button").addEventListener("click", () => sendPtz("stop"));
$("#ptz-speed").addEventListener("input", (event) => {
  $("#ptz-speed-value").textContent = `${new Intl.NumberFormat("fa-IR").format(event.target.value)}٪`;
});

$("#settings-profile").addEventListener("change", renderSettingsProfile);
$("#settings-quality").addEventListener("input", (event) => {
  $("#settings-quality-value").textContent = event.target.value;
});
$("#settings-gop").addEventListener("input", (event) => {
  $("#settings-gop-value").textContent = event.target.value;
});

$("#record-browse").addEventListener("click", async () => {
  const button = $("#record-browse");
  button.disabled = true;
  try {
    const result = await api("/api/recording/browse", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (result.folder) $("#record-folder").value = result.folder;
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#record-settings-save").addEventListener("click", async () => {
  const button = $("#record-settings-save");
  const message = $("#record-settings-message");
  button.disabled = true;
  message.className = "settings-message";
  message.textContent = "در حال ذخیره‌ی تنظیمات ضبط...";
  try {
    const result = await api("/api/recording/settings", {
      method: "POST",
      body: JSON.stringify({
        folder: $("#record-folder").value,
        segmentMinutes: Number($("#record-minutes").value),
      }),
    });
    $("#record-folder").value = result.settings.folder;
    $("#record-minutes").value = result.settings.segmentMinutes;
    message.className = "settings-message success";
    message.textContent = "تنظیمات ضبط ذخیره شد.";
    await refreshRecording();
  } catch (error) {
    message.className = "settings-message error";
    message.textContent = `ذخیره ناموفق بود: ${error.message}`;
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#record-button").addEventListener("click", async () => {
  const button = $("#record-button");
  button.disabled = true;
  try {
    const endpoint = recordingState?.recording
      ? "/api/recording/stop"
      : "/api/recording/start";
    const result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ profile: selectedProfile() }),
    });
    updateRecordingUI(result.status);
    notify(result.status.recording ? "ضبط محلی آغاز شد." : "ضبط متوقف و فایل نهایی شد.");
  } catch (error) {
    notify(error.message, true);
    await refreshRecording();
  }
});

$("#settings-refresh").addEventListener("click", refreshSettings);
$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const profile = settingsState.find((item) => item.profileToken === $("#settings-profile").value);
  if (!profile) return;
  const [width, height] = $("#settings-resolution").value.split("x").map(Number);
  const button = $("#settings-save");
  button.disabled = true;
  $("#settings-message").className = "settings-message";
  $("#settings-message").textContent = "در حال ذخیره...";
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        profileToken: profile.profileToken,
        resolution: { width, height },
        frameRate: Number($("#settings-fps").value),
        quality: Number($("#settings-quality").value),
        gop: Number($("#settings-gop").value),
      }),
    });
    $("#settings-message").className = "settings-message success";
    $("#settings-message").textContent = result.message;
    notify(result.message);
    await refreshSettings();
    await refreshStatus();
  } catch (error) {
    $("#settings-message").className = "settings-message error";
    $("#settings-message").textContent = `ذخیره ناموفق بود: ${error.message}`;
    notify(error.message, true);
    button.disabled = false;
  }
});

$("#relay-on").addEventListener("click", async () => {
  if (!window.confirm("فعال‌کردن رله ممکن است دستگاه متصل به دوربین را روشن کند. ادامه می‌دهید؟")) return;
  try {
    const result = await api("/api/relay", {
      method: "POST",
      body: JSON.stringify({ state: "active" }),
    });
    notify(result.message);
  } catch (error) {
    notify(error.message, true);
  }
});

$("#relay-off").addEventListener("click", async () => {
  try {
    const result = await api("/api/relay", {
      method: "POST",
      body: JSON.stringify({ state: "inactive" }),
    });
    notify(result.message);
  } catch (error) {
    notify(error.message, true);
  }
});

refreshStatus();
refreshSettings();
refreshRecording();
setInterval(refreshStatus, 60_000);
setInterval(refreshRecording, 2000);
