(function () {
  "use strict";

  function applyResult(form, result) {
    var item = form.closest("[data-vote-item]");
    if (!item) return;
    item.querySelectorAll("button.vote-btn").forEach(function (b) {
      var own = b.dataset.score;
      var active = result.score !== null && String(result.score) === own;
      b.value = active ? "" : own;          // un second clic sur le choix actif retire le vote
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    var total = item.querySelector("[data-vote-total]");
    if (total) total.textContent = result.total + " pt" + (result.total === 1 ? "" : "s");
    var counts = item.querySelector("[data-vote-counts]");
    if (counts) {
      counts.textContent = "Oui " + result.yes + " · Peut-être " + result.maybe +
                           " · Non " + result.no;
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("button.vote-btn");
    if (!button || !button.form || !window.fetch || !window.FormData) return;
    event.preventDefault();
    var form = button.form;
    var data = new FormData(form);
    data.set("score", button.value);
    button.disabled = true;
    fetch(form.action, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" }
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (result) {
      applyResult(form, result);
    }).catch(function () {
      // Repli : envoi classique du formulaire (la page se recharge sur le bon bloc).
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "score";
      hidden.value = button.value;
      form.appendChild(hidden);
      form.submit();
    }).then(function () {
      button.disabled = false;
    });
  });
})();
