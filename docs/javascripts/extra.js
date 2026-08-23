// Adds a "Copy for uv" button next to any code block tagged with a `data-uv-extra` attribute
// (see docs/index.md's Quickstart tabs). Clicking it copies a `uv run --with 'lothc[...]'`
// one-liner that runs that exact snippet, no local install needed.
//
// Written independently of zensical's own built-in copy/select buttons (`.md-code__nav`) rather
// than joining that container, since it's injected client-side with no server-rendered markup to
// inspect ahead of time — inserting our own button as a sibling above the block sidesteps having
// to guess at that internal structure.
(() => {
  "use strict"

  const PLAY_ICON = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"></path></svg>'
  const HEREDOC_MARKER = "PYEOF"

  function buildCommand(extra, code) {
    const target = extra ? `lothc[${extra}]` : "lothc"
    const body = code.replace(/\n+$/, "")
    return `uv run --with '${target}' python - <<'${HEREDOC_MARKER}'\n${body}\n${HEREDOC_MARKER}\n`
  }

  function copyToClipboard(text) {
    // GitHub Pages serves this site over HTTPS, and the Clipboard API requires a secure
    // context, so there's no legacy execCommand("copy") fallback to maintain here.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text)
    }
    return Promise.reject(new Error("Clipboard API unavailable"))
  }

  function enhance(block) {
    if (block.dataset.uvEnhanced) {
      return
    }
    block.dataset.uvEnhanced = "1"

    const codeEl = block.querySelector("code")
    if (!codeEl) {
      return
    }

    const button = document.createElement("button")
    button.type = "button"
    button.className = "md-uv-run"
    button.title = "Copy a uv run command for this script"

    const label = document.createElement("span")
    label.textContent = "Copy for uv"
    button.innerHTML = PLAY_ICON
    button.appendChild(label)

    button.addEventListener("click", () => {
      const command = buildCommand(block.dataset.uvExtra, codeEl.textContent || "")
      copyToClipboard(command).then(() => {
        const original = label.textContent
        label.textContent = "Copied!"
        button.classList.add("md-uv-run--copied")
        window.setTimeout(() => {
          label.textContent = original
          button.classList.remove("md-uv-run--copied")
        }, 1500)
      }).catch((error) => {
        console.error("lothc docs: couldn't copy uv run command", error)
      })
    })

    block.parentNode.insertBefore(button, block)
  }

  function scan(root) {
    root.querySelectorAll("[data-uv-extra]").forEach(enhance)
  }

  function start() {
    const content = document.querySelector('[data-md-component="content"]') || document.body
    scan(content)

    // `navigation.instant` swaps page content via fetch, not a full reload, so new
    // `[data-uv-extra]` blocks can appear without DOMContentLoaded firing again.
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        mutation.addedNodes.forEach((node) => {
          if (node.nodeType !== 1) {
            return
          }
          if (node.matches && node.matches("[data-uv-extra]")) {
            enhance(node)
          }
          if (node.querySelectorAll) {
            scan(node)
          }
        })
      }
    }).observe(content, { childList: true, subtree: true })
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start)
  } else {
    start()
  }
})()
