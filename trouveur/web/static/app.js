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
