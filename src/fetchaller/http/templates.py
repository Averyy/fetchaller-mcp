"""HTML templates for OAuth authorization pages."""

from ..security.xss import escape_html


def get_authorize_page(
    client_id: str,
    redirect_uri: str,
    state: str | None,
    code_challenge: str,
    error: str | None = None,
) -> str:
    """
    Generate the OAuth authorization page HTML.

    All user-controlled values are HTML-escaped to prevent XSS.
    """
    safe_client_id = escape_html(client_id)
    safe_redirect_uri = escape_html(redirect_uri)
    safe_state = escape_html(state or "")
    safe_code_challenge = escape_html(code_challenge)
    error_html = f'<div class="error" role="alert" aria-live="assertive">{escape_html(error)}</div>' if error else ""

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Authorize | fetchaller</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-primary: #000000;
      --bg-secondary: #0a0a0a;
      --bg-card: rgba(255, 255, 255, 0.03);
      --border-color: rgba(255, 255, 255, 0.08);
      --border-color-hover: rgba(255, 255, 255, 0.15);
      --text-primary: #f5f5f7;
      --text-secondary: #a1a1a6;
      --text-tertiary: #8e8e93;
      --accent: #ff2300;
      --accent-hover: #cc1c00;
      --purple: #62007f;
      --red: #ff453a;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      -webkit-font-smoothing: antialiased;
    }}
    .gradient-bg {{
      position: fixed;
      top: 0; left: 0; right: 0;
      height: 100vh;
      background:
        radial-gradient(ellipse 60% 40% at 25% -10%, rgba(98, 0, 127, 0.15), transparent),
        radial-gradient(ellipse 50% 35% at 75% 20%, rgba(255, 35, 0, 0.12), transparent);
      pointer-events: none;
      z-index: -1;
    }}
    .card {{
      width: 100%;
      max-width: 400px;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 32px;
    }}
    .logo {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
      margin-bottom: 24px;
    }}
    .logo-icon {{
      width: 40px;
      height: 40px;
      border-radius: 10px;
      overflow: hidden;
    }}
    .logo-icon svg {{
      width: 100%;
      height: 100%;
    }}
    .logo-text {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.5rem;
      font-weight: 600;
    }}
    h1 {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.25rem;
      font-weight: 600;
      text-align: center;
      margin-bottom: 8px;
    }}
    .subtitle {{
      color: var(--text-secondary);
      text-align: center;
      font-size: 0.9375rem;
      margin-bottom: 24px;
      line-height: 1.5;
    }}
    .form-group {{ margin-bottom: 20px; }}
    label {{
      display: block;
      font-size: 0.875rem;
      font-weight: 500;
      margin-bottom: 8px;
      color: var(--text-secondary);
    }}
    input[type="password"], input[type="text"] {{
      width: 100%;
      padding: 14px 16px;
      background: var(--bg-secondary);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      color: var(--text-primary);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.9375rem;
      transition: border-color 0.2s;
    }}
    input:focus {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
      border-color: var(--accent);
    }}
    input::placeholder {{
      color: var(--text-tertiary);
    }}
    .btn {{
      width: 100%;
      padding: 14px 24px;
      background: var(--accent);
      border: none;
      border-radius: 10px;
      color: white;
      font-family: 'Space Grotesk', sans-serif;
      font-size: 1rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .btn:hover {{
      background: var(--accent-hover);
      transform: translateY(-1px);
    }}
    .btn:focus {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}
    .btn:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }}
    .error {{
      background: rgba(255, 69, 58, 0.1);
      border: 1px solid rgba(255, 69, 58, 0.3);
      color: var(--red);
      padding: 12px 16px;
      border-radius: 10px;
      font-size: 0.875rem;
      margin-bottom: 20px;
      text-align: center;
    }}
    .info {{
      margin-top: 20px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-color);
      border-radius: 10px;
    }}
    .info-title {{
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }}
    .info-text {{
      font-size: 0.8125rem;
      color: var(--text-tertiary);
      line-height: 1.5;
    }}
    .info-text code {{
      background: var(--bg-secondary);
      padding: 2px 6px;
      border-radius: 4px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
    }}
    .client-info {{
      font-size: 0.75rem;
      color: var(--text-tertiary);
      text-align: center;
      margin-top: 16px;
    }}
    .skip-link {{
      position: absolute;
      left: -9999px;
      top: auto;
      width: 1px;
      height: 1px;
      overflow: hidden;
    }}
    .skip-link:focus {{
      position: fixed;
      top: 10px;
      left: 10px;
      width: auto;
      height: auto;
      padding: 8px 16px;
      background: var(--accent);
      color: white;
      text-decoration: none;
      border-radius: 8px;
      z-index: 1000;
    }}
  </style>
</head>
<body>
  <a href="#authForm" class="skip-link">Skip to form</a>
  <div class="gradient-bg"></div>
  <main class="card" id="main-content">
    <div class="logo">
      <div class="logo-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" aria-hidden="true"><defs><linearGradient id="lg" x1="33.29" y1="363.29" x2="478.71" y2="808.71" gradientTransform="translate(0 -330)" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#62007f"/><stop offset="1" stop-color="#ff2300"/></linearGradient></defs><rect fill="url(#lg)" width="512" height="512" rx="113.66" ry="113.66"/><path fill="#fff" d="M206.94,428.01v-32.65h-.01v-178.34h-57.99v-32.65h57.99v-13.64c0-30.53,7.96-52.63,23.88-66.27,15.91-13.64,38.65-20.47,68.22-20.47,10.72,0,20.3.66,28.75,1.95,8.44,1.3,17.54,3.9,27.29,7.8l-8.73,31.67c-8.41-3.57-16.5-5.92-24.26-7.07-7.77-1.13-14.88-1.7-21.34-1.7-21.35,0-35.91,5.28-43.67,15.84-7.77,10.56-11.64,27.86-11.64,51.9h100.87v32.65h-103.87l3,178.34h100.87v32.65h-139.35Z"/></svg></div>
      <span class="logo-text">fetchaller</span>
    </div>
    <h1>Connect to fetchaller</h1>
    <p class="subtitle">Enter your API key to authorize Claude to use fetchaller on your behalf.</p>
    {error_html}
    <form method="POST" action="/authorize" id="authForm">
      <input type="hidden" name="client_id" value="{safe_client_id}">
      <input type="hidden" name="redirect_uri" value="{safe_redirect_uri}">
      <input type="hidden" name="state" value="{safe_state}">
      <input type="hidden" name="code_challenge" value="{safe_code_challenge}">
      <div class="form-group">
        <label for="api_key">API Key</label>
        <input type="password" id="api_key" name="api_key" placeholder="Enter your MCP_API_KEY" required autocomplete="off">
      </div>
      <button type="submit" class="btn" id="submitBtn">Authorize</button>
    </form>
    <script>
      document.getElementById('authForm').addEventListener('submit', function() {{
        var btn = document.getElementById('submitBtn');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        btn.textContent = 'Authorizing...';
      }});
    </script>
    <div class="info">
      <div class="info-title">What is this?</div>
      <div class="info-text">
        This authorizes Claude to fetch web pages through your fetchaller server.
        Your API key is the <code>MCP_API_KEY</code> you set when deploying the server.
      </div>
    </div>
    <div class="client-info">
      Requesting access: Claude
    </div>
  </main>
</body>
</html>'''
