"""Tests for Hugging Face markdown post-processing — outcome-level tests."""

from fetchaller.content.html import html_to_markdown


class TestHuggingFaceModelCard:
    """A HF model card page should come out clean."""

    async def test_header_chrome_stripped(self):
        """Main header, theme switcher, and SSO banner are stripped."""
        html = """<body>
        <div data-target="MainHeader">
            <a href="/"><img alt="Hugging Face's logo" src="/logo.svg"/>Hugging Face</a>
            <a href="/models">Models</a><a href="/datasets">Datasets</a>
            <a href="/login">Log In</a><a href="/join">Sign Up</a>
        </div>
        <div data-target="ThemeSwitcher">System theme</div>
        <div data-target="SSOBanner"></div>
        <h1>Model Card Content</h1>
        <p>This model does something useful.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/org/model")
        assert "Log In" not in md
        assert "Sign Up" not in md
        assert "System theme" not in md
        assert "Model Card Content" in md
        assert "something useful" in md

    async def test_filter_tags_stripped(self):
        """Filter tag links like [Text Generation](/models?pipeline_tag=...) are stripped."""
        html = """<body>
        <a href="/models?pipeline_tag=text-generation">Text Generation</a>
        <a href="/models?library=transformers">Transformers</a>
        <a href="/models?library=safetensors">Safetensors</a>
        <a href="/models?language=en">English</a>
        <h2>Model Summary</h2>
        <p>A 2.7B parameter model.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/microsoft/phi-2")
        assert "pipeline_tag" not in md
        assert "[Text Generation]" not in md
        assert "[Transformers]" not in md
        assert "Model Summary" in md
        assert "2.7B parameter" in md

    async def test_inference_widget_stripped(self):
        """Inference widget and 'not deployed' message are stripped."""
        html = """<body>
        <div data-target="InferenceWidget">
            <p>Inference Providers NEW</p>
            <p>This model isn't deployed by any Inference Provider.</p>
        </div>
        <h2>How to Use</h2>
        <p>Install transformers and run the model.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/org/model")
        assert "Inference Provider" not in md
        assert "isn't deployed" not in md
        assert "How to Use" in md
        assert "Install transformers" in md

    async def test_deploy_buttons_stripped(self):
        """Deploy and Use this model buttons are stripped."""
        html = """<body>
        <button>Deploy</button>
        <button>Use this model</button>
        <h2>Model Architecture</h2>
        <p>Transformer-based model.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/org/model")
        assert "Deploy" not in md
        assert "Use this model" not in md
        assert "Model Architecture" in md

    async def test_like_follow_stripped(self):
        """Like and Follow buttons are stripped."""
        html = """<body>
        <button>like</button>
        <button>3.42k</button>
        <button>Follow Microsoft</button>
        <button>18.3k</button>
        <h2>About</h2>
        <p>Model description here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/org/model")
        assert "3.42k" not in md
        assert "18.3k" not in md
        assert "Model description" in md

    async def test_linked_spaces_stripped(self):
        """LinkedSpacesList is stripped."""
        html = """<body>
        <div data-target="LinkedSpacesList">
            <a href="/spaces/user/app1">App 1</a>
            <a href="/spaces/user/app2">App 2</a>
        </div>
        <p>Model content here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/org/model")
        assert "App 1" not in md
        assert "Model content" in md

    async def test_model_content_preserved(self):
        """Model card content including code blocks is preserved."""
        html = """<body>
        <div data-target="MainHeader"><a href="/">HF</a></div>
        <h2>How to Use</h2>
        <pre><code class="language-python">from transformers import AutoModelForCausalLM
model = AutoModelForCausalLM.from_pretrained("org/model")</code></pre>
        <h2>Benchmarks</h2>
        <table><tr><th>Task</th><th>Score</th></tr><tr><td>MMLU</td><td>78.5</td></tr></table>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/org/model")
        assert "from transformers import" in md
        assert "MMLU" in md
        assert "78.5" in md


class TestHuggingFaceDatasetPage:
    """Dataset pages should have the viewer stripped."""

    async def test_dataset_viewer_stripped(self):
        """DatasetViewer is stripped (can be 192k+ chars)."""
        html = """<body>
        <div data-target="DatasetViewer">
            <p>Subset (114)</p>
            <p>default (25.9B rows)</p>
            <p>CC-MAIN-2013-20 (215M rows)</p>
            <p>CC-MAIN-2024-51 (179M rows)</p>
            <table><tr><td>text</td><td>id</td><td>dump</td></tr></table>
        </div>
        <h1>FineWeb</h1>
        <p>15 trillion tokens of the finest data the web has to offer.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/datasets/org/dataset")
        assert "CC-MAIN" not in md
        assert "25.9B rows" not in md
        assert "Subset (114)" not in md
        assert "FineWeb" in md
        assert "15 trillion tokens" in md


class TestHuggingFaceOrgPage:
    """Org pages should preserve the org description."""

    async def test_org_content_preserved(self):
        """The org description is preserved."""
        html = """<body>
        <div data-target="MainHeader"><a href="/">HF</a></div>
        <div data-target="OrgHeaderActions"><button>Follow</button></div>
        <h1>The Llama Family</h1>
        <p>Welcome to the official Hugging Face organization for Llama models from Meta!</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/meta-llama")
        assert "Llama Family" in md
        assert "official Hugging Face organization" in md


class TestHuggingFaceGatedModel:
    """Gated models should have the license agreement block stripped."""

    async def test_license_gate_stripped(self):
        """The login gate and license block are stripped."""
        html = """<body>
        <h2>You need to agree to share your contact information to access this model</h2>
        <p>The information you provide will be collected per the Meta Privacy Policy.</p>
        <h3>LLAMA 3.3 COMMUNITY LICENSE AGREEMENT</h3>
        <p>Llama 3.3 Version Release Date: December 6, 2024</p>
        <p>1. License Rights and Redistribution. You are granted a non-exclusive license.</p>
        <h3>Llama 3.3 Acceptable Use Policy</h3>
        <p>Meta is committed to promoting safe use of its tools.</p>
        <p>Prohibited Uses: violence, exploitation, trafficking.</p>
        <a href="/login">Log in</a> or <a href="/join">Sign Up</a>
        <p>to review the conditions and access this model content.</p>
        <h2>Model Information</h2>
        <p>The Meta Llama 3.3 multilingual large language model.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct")
        assert "LICENSE AGREEMENT" not in md
        assert "Acceptable Use Policy" not in md
        assert "Prohibited Uses" not in md
        assert "Log in" not in md
        assert "Model Information" in md
        assert "multilingual large language model" in md


class TestHuggingFaceIsolation:
    """HF post-processing must not affect other sites."""

    async def test_non_hf_url_unaffected(self):
        """Non-HF URLs should not be affected."""
        html = """<body>
        <button>Deploy</button>
        <button>Follow</button>
        <p>Sign Up</p>
        <p>Real content</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://example.com/page")
        assert "Real content" in md

    async def test_no_url_unaffected(self):
        """No URL should not trigger HF processing."""
        html = "<body><p>Deploy</p><p>Follow</p></body>"
        md, _ = await html_to_markdown(html)
        assert "Deploy" in md
        assert "Follow" in md
