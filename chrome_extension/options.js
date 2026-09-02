// DeepFlux Integration Module — Options Page Script

document.addEventListener("DOMContentLoaded", () => {
  // Load saved settings.
  chrome.storage.sync.get({
    autoCapture: false,
    sizeThreshold: 10 * 1024 * 1024,
    showOverlay: true
  }, (settings) => {
    document.getElementById("autoCapture").checked = settings.autoCapture;
    const bytes = settings.sizeThreshold < 1024 ? settings.sizeThreshold * 1024 * 1024 : settings.sizeThreshold;
    document.getElementById("sizeThreshold").value = Math.max(1, Math.round(bytes / (1024 * 1024)));
    document.getElementById("showOverlay").checked = settings.showOverlay;
  });

  // Show this extension's ID.
  const idEl = document.getElementById("extensionId");
  idEl.value = chrome.runtime.id;

  // Save settings.
  document.getElementById("save").addEventListener("click", () => {
    const settings = {
      autoCapture: document.getElementById("autoCapture").checked,
      sizeThreshold: (parseInt(document.getElementById("sizeThreshold").value) || 10) * 1024 * 1024,
      showOverlay: document.getElementById("showOverlay").checked
    };
    chrome.storage.sync.set(settings, () => {
      const savedEl = document.getElementById("saved");
      savedEl.style.display = "inline";
      setTimeout(() => {
        savedEl.style.display = "none";
      }, 2000);
    });
  });
});
