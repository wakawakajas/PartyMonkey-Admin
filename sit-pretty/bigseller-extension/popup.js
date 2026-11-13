const $ = id => document.getElementById(id);
chrome.storage.local.get(['host', 'pin']).then(c => { $('host').value = c.host || 'DESKTOP-ABGC9KO:8787'; $('pin').value = c.pin || ''; });
$('save').onclick = async () => {
  await chrome.storage.local.set({ host: $('host').value.trim() || 'DESKTOP-ABGC9KO:8787', pin: $('pin').value.trim() });
  $('msg').className = ''; $('msg').textContent = 'Testing…';
  chrome.runtime.sendMessage({ type: 'test' }, res => {
    if (res && !res.error) { $('msg').className = 'ok'; $('msg').textContent = 'Connected to Sit Pretty. Reload the BigSeller tab.'; }
    else if (res && res.error === 'pin') { $('msg').className = 'err'; $('msg').textContent = 'Wrong PIN.'; }
    else { $('msg').className = 'err'; $('msg').textContent = 'Cannot reach Sit Pretty. Is it running on that PC? (' + ((res && res.error) || 'no answer') + ')'; }
  });
};
