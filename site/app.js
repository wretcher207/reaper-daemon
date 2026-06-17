(function () {
  // Copy install command to clipboard
  const btn = document.getElementById('copyBtn');
  const code = document.getElementById('installCmd');
  if (btn && code) {
    const label = btn.querySelector('.copy-label');
    btn.addEventListener('click', async () => {
      const text = code.textContent.trim();
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        // Fallback for older browsers / non-secure contexts: select the text.
        const range = document.createRange();
        range.selectNodeContents(code);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        document.execCommand && document.execCommand('copy');
        sel.removeAllRanges();
      }
      const original = label.textContent;
      label.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        label.textContent = original;
        btn.classList.remove('copied');
      }, 1500);
    });
  }

  // The literal dead pixel — drifts to a new spot every ~1.4s, occasionally flickers off.
  const pixel = document.getElementById('deadPixel');
  if (pixel && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const move = () => {
      const x = Math.random() * window.innerWidth;
      const y = Math.random() * window.innerHeight;
      pixel.style.left = x + 'px';
      pixel.style.top = y + 'px';
      pixel.style.opacity = Math.random() > 0.25 ? '1' : '0';
    };
    move();
    setInterval(move, 1400);
  }
})();
