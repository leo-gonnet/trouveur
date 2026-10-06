// Chip editing for `.tags` list fields. The textarea stays the source of truth and is what the
// form posts, one value per line; the chips are a view of it, so the page works without this.
(function () {
  function labelsOf(textarea) {
    const list = document.getElementById(textarea.getAttribute("list") || "");
    if (!list) return null;
    const map = new Map();
    for (const option of list.options) map.set(option.value, option.textContent);
    return map;
  }

  function enhance(root) {
    const textarea = root.querySelector("textarea");
    const labels = labelsOf(textarea);
    const field = document.createElement("div");
    field.className = "tags-field";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = textarea.dataset.placeholder || "";
    input.autocomplete = "off";
    if (labels) input.setAttribute("list", textarea.getAttribute("list"));
    field.appendChild(input);
    root.appendChild(field);
    root.dataset.enhanced = "";

    let values = textarea.value.split("\n").map((v) => v.trim()).filter(Boolean);

    function resolve(text) {
      const wanted = text.trim();
      if (!wanted) return null;
      if (!labels) return wanted;
      const folded = wanted.toLowerCase();
      for (const [code, name] of labels) {
        if (code.toLowerCase() === folded || name.toLowerCase() === folded) return code;
      }
      return null;
    }

    function render() {
      textarea.value = values.join("\n");
      field.querySelectorAll(".pill").forEach((chip) => chip.remove());
      values.forEach((value, index) => {
        const chip = document.createElement("span");
        chip.className = "pill";
        chip.append(labels ? labels.get(value) || value : value);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", "remove " + value);
        remove.textContent = "×";
        remove.addEventListener("click", () => { values.splice(index, 1); render(); });
        chip.appendChild(remove);
        field.insertBefore(chip, input);
      });
    }

    function add(text) {
      const value = resolve(text);
      if (value === null) {
        input.setAttribute("aria-invalid", text.trim() ? "true" : "false");
        return false;
      }
      if (!values.includes(value)) values.push(value);
      input.value = "";
      input.removeAttribute("aria-invalid");
      render();
      return true;
    }

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        add(input.value);
      } else if (event.key === "Backspace" && input.value === "" && values.length) {
        values.pop();
        render();
      }
    });
    input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
    if (textarea.dataset.suggest && labels) {
      const list = document.getElementById(textarea.getAttribute("list"));
      let timer = null;
      input.addEventListener("input", () => {
        clearTimeout(timer);
        const query = input.value.trim();
        // A picked suggestion puts its id in the box; that is not something to search for.
        if (query.length < 2 || /^\d+$/.test(query)) return;
        timer = setTimeout(async () => {
          const response = await fetch(textarea.dataset.suggest + "?q=" + encodeURIComponent(query));
          if (!response.ok) return;
          list.innerHTML = await response.text();
          for (const option of list.options) labels.set(option.value, option.textContent);
        }, 150);
      });
    }
    // A datalist pick fires `change` without a keystroke.
    input.addEventListener("change", () => { if (labels) add(input.value); });
    input.addEventListener("paste", (event) => {
      const text = (event.clipboardData || window.clipboardData).getData("text");
      if (!/[\n,]/.test(text)) return;
      event.preventDefault();
      text.split(/[\n,]/).forEach(add);
    });
    field.addEventListener("click", () => input.focus());
    // Whatever is still typed when the form goes counts as entered.
    textarea.form.addEventListener("submit", () => { if (input.value.trim()) add(input.value); });

    render();
  }

  document.querySelectorAll(".tags").forEach(enhance);
})();

// htmx leaves the DOM untouched and says nothing when a swap fails, so pressing a state button
// against a 500 or a dropped connection looks exactly like pressing it successfully.
(function () {
  let holder = null;
  let timer = null;

  function show(message) {
    if (!holder) {
      holder = document.createElement("div");
      holder.className = "toast";
      holder.setAttribute("role", "status");
      holder.setAttribute("aria-live", "polite");
      document.body.appendChild(holder);
    }
    holder.textContent = message;
    holder.hidden = false;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => { holder.hidden = true; }, 6000);
  }

  document.body.addEventListener("htmx:responseError", (event) => {
    const status = event.detail.xhr.status;
    show(status === 404
      ? "That posting is no longer there. Reload the page."
      : `That did not save (error ${status}). Nothing was changed; try again.`);
  });
  document.body.addEventListener("htmx:sendError", () => {
    show("Could not reach Trouveur. Nothing was changed; check your connection.");
  });
  document.body.addEventListener("htmx:timeout", () => {
    show("That timed out. Nothing was changed; try again.");
  });
})();

// <details> opens, closes on a second click and closes on the next navigation by itself. It does
// not come with these two, and `contains` is what keeps them from undoing the click that opened it.
(function () {
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelectorAll("details.menu[open]").forEach((menu) => {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    });
  });

  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
})();
