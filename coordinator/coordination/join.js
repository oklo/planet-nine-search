'use strict';

const $ = id => document.getElementById(id);
const copyPrompt = $('copy-worker-prompt');
if (navigator.clipboard) {
  copyPrompt.hidden = false;
  copyPrompt.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('worker-prompt').textContent);
      $('copy-status').textContent = 'Copied.';
    } catch {
      $('copy-status').textContent = 'Select the prompt above and copy it.';
    }
  });
}
