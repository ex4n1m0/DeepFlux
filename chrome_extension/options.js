// DeepFlux Integration Module — Options Page Script

document.addEventListener("DOMContentLoaded", () => {
  // Load saved settings.
  chrome.storage.sync.get({
    autoCapture: false,
    sizeThreshold: 10,
    showOverlay: true
  }, (settings) => {
    document.getElementById("autoCapture").checked = settings.autoCapture;
    document.getElementById("sizeThreshold").value = settings.sizeThreshold;
    document.getElementById("showOverlay").checked = settings.showOverlay;
  });

  // Show this extension's ID.
  const idEl = document.getElementById("extensionId");
  idEl.value = chrome.runtime.id;

  // Save settings.
  document.getElementById("save").addEventListener("click", () => {
    const settings = {
      autoCapture: document.getElementById("autoCapture").checked,
      sizeThreshold: parseInt(document.getElementById("sizeThreshold").value) || 10,
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
