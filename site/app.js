(function () {
  "use strict";

  /* ------------------------------------------------------------
     Reveal on scroll — groups rise in as they enter the viewport.
     Purely progressive: without JS everything is already visible
     (the hiding styles are gated behind html.js).
     ------------------------------------------------------------ */
  var revealEls = Array.prototype.slice.call(
    document.querySelectorAll(".reveal-group")
  );

  var reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  if ("IntersectionObserver" in window && !reduceMotion && revealEls.length) {
    var io = new IntersectionObserver(
      function (entries) {
        var batch = 0;
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var el = entry.target;
          el.style.transitionDelay = Math.min(batch * 90, 270) + "ms";
          batch += 1;
          el.classList.add("in");
          io.unobserve(el);
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" }
    );
    revealEls.forEach(function (el) {
      io.observe(el);
    });
  } else {
    revealEls.forEach(function (el) {
      el.classList.add("in");
    });
  }

  /* ------------------------------------------------------------
     Copy buttons. State tells the truth: "Copied" appears only
     after a copy actually succeeded.
     ------------------------------------------------------------ */
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (_) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok;
  }

  var btns = Array.prototype.slice.call(document.querySelectorAll(".copy-btn"));

  btns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = document.querySelector(btn.getAttribute("data-copy"));
      if (!target) return;
      var text = target.textContent.trim();
      var label = btn.querySelector("span");

      function done(ok) {
        if (!ok) return;
        btn.classList.add("copied");
        if (label) label.textContent = "Copied";
        setTimeout(function () {
          btn.classList.remove("copied");
          if (label) label.textContent = "Copy";
        }, 1600);
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () {
            done(true);
          },
          function () {
            done(legacyCopy(text));
          }
        );
      } else {
        done(legacyCopy(text));
      }
    });
  });

  /* ------------------------------------------------------------
     Footer year, kept current without a build step.
     ------------------------------------------------------------ */
  var yearEl = document.querySelector("[data-year]");
  if (yearEl) yearEl.textContent = String(new Date().getFullYear());
})();
