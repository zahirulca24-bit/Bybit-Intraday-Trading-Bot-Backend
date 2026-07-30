from pathlib import Path

path = Path(__file__).resolve().parents[1] / "frontend" / "index.html"
html = path.read_text(encoding="utf-8")
marker = "button-state-ui-v1"
if marker not in html:
    html = html.replace(
        'const autoStartBtn = document.getElementById("autoStartBtn");\n      const startBtn = document.getElementById("startBtn");',
        'const autoStartBtn = document.getElementById("autoStartBtn");\n      const autoStopBtn = document.getElementById("autoStopBtn");\n      const startBtn = document.getElementById("startBtn");',
        1,
    )
    html = html.replace(
        'if (autoStartBtn) autoStartBtn.disabled = false;\n        if (startBtn) startBtn.disabled = false;',
        '// button-state-ui-v1\n        if (autoStartBtn) autoStartBtn.disabled = Boolean(bot.enabled);\n        if (autoStopBtn) autoStopBtn.disabled = !Boolean(bot.enabled);\n        if (startBtn) startBtn.disabled = Boolean(bot.enabled);',
        1,
    )
    html = html.replace(
        'if (autoStartBtn) autoStartBtn.disabled = true;\n        if (startBtn) startBtn.disabled = true;',
        'if (autoStartBtn) autoStartBtn.disabled = true;\n        if (autoStopBtn) autoStopBtn.disabled = false;\n        if (startBtn) startBtn.disabled = true;',
        1,
    )
    if marker not in html:
        raise RuntimeError("button state anchors missing")
    path.write_text(html, encoding="utf-8")
    print("Applied runtime button state UI patch")
else:
    print("Runtime button state UI patch already applied")
