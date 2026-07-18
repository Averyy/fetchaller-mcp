"""Unit tests for Lever content module."""

from fetchaller.content.lever import (
    extract_lever_params,
    is_lever_url,
    parse_lever_apply_form,
    render_lever_job,
)


class TestIsLeverUrl:
    def test_posting(self):
        assert is_lever_url(
            "https://jobs.lever.co/includedhealth/8cf2e124-00b8-4aba-a7b0-56a1b5fd0000"
        )

    def test_apply(self):
        assert is_lever_url(
            "https://jobs.lever.co/includedhealth/8cf2e124-00b8-4aba-a7b0-56a1b5fd0000/apply"
        )

    def test_not_lever(self):
        assert not is_lever_url("https://example.com/jobs/123")

    def test_extract(self):
        assert extract_lever_params(
            "https://jobs.lever.co/includedhealth/8cf2e124-00b8-4aba-a7b0-56a1b5fd0000"
        ) == ("includedhealth", "8cf2e124-00b8-4aba-a7b0-56a1b5fd0000")


class TestParseLeverApplyForm:
    def test_sections_preserved(self):
        html = """
        <html><body>
          <section class="section application-form">
            <ul>
              <li class="application-question resume">
                <label>
                  <div class="application-label">Resume/CV<span class="required">✱</span></div>
                  <div class="application-field"><input type="file" name="resume"/></div>
                </label>
              </li>
              <li class="application-question custom-question">
                <label>
                  <div class="application-label full-width multiple-choice">Sponsorship?<span class="required">✱</span></div>
                  <div class="application-answer-alternative">Yes</div>
                  <div class="application-answer-alternative">No</div>
                </label>
              </li>
            </ul>
          </section>
          <section class="section eeo-section application-form">
            <ul>
              <li class="application-question">
                <label>
                  <div class="application-label full-width dropdown">Gender</div>
                  <select>
                    <option value="">Select ...</option>
                    <option>Male</option>
                    <option>Female</option>
                  </select>
                </label>
              </li>
            </ul>
          </section>
        </body></html>
        """
        sections = parse_lever_apply_form(html)
        # Main section + EEO section
        assert len(sections) == 2
        titles = [s["title"] for s in sections]
        assert "eeo" in titles  # Lever's own class suffix preserved
        main = [s for s in sections if s["title"] != "eeo"][0]
        labels = {q["label"] for q in main["questions"]}
        assert "Resume/CV" in labels
        assert "Sponsorship?" in labels
        sponsor = [q for q in main["questions"] if q["label"] == "Sponsorship?"][0]
        assert sponsor["options"] == ["Yes", "No"]
        eeo = [s for s in sections if s["title"] == "eeo"][0]
        gender = eeo["questions"][0]
        assert gender["label"] == "Gender"
        assert "Male" in gender["options"] and "Female" in gender["options"]

    def test_required_detected_via_field_class(self):
        # Regression: required marked ONLY by the .application-field.required-field
        # class (no <span class="required">). A double " ".join used to char-explode
        # the class string, so this path always read as optional.
        html = """
        <html><body>
          <section class="section application-form">
            <ul>
              <li class="application-question">
                <label>
                  <div class="application-label">Full name</div>
                  <div class="application-field required-field"><input type="text" name="name"/></div>
                </label>
              </li>
              <li class="application-question">
                <label>
                  <div class="application-label">LinkedIn</div>
                  <div class="application-field"><input type="text" name="urls[LinkedIn]"/></div>
                </label>
              </li>
            </ul>
          </section>
        </body></html>
        """
        sections = parse_lever_apply_form(html)
        questions = {q["label"]: q for q in sections[0]["questions"]}
        assert questions["Full name"]["required"] is True
        assert questions["LinkedIn"]["required"] is False


class TestRenderLeverJob:
    def test_preserves_raw_fields_and_lists(self):
        data = {
            "posting": {
                "text": "Staff Product Designer",
                "hostedUrl": "https://jobs.lever.co/acme/abc-123/",
                "categories": {
                    "commitment": "Full-Time",
                    "department": "Product & Design",
                    "team": "Design",
                    "location": "Remote",
                    "allLocations": ["Remote", "San Francisco"],
                },
                "workplaceType": "remote",
                "country": "US",
                "createdAt": "1774635948824",
                "description": "<p>Lead design.</p>",
                "descriptionBody": "<p>Lead design.</p>",
                "descriptionBodyPlain": "Lead design.",
                "opening": "",
                "lists": [
                    {"text": "Responsibilities:", "content": "<li>Design</li><li>Ship</li>"},
                ],
                "additional": "<p>Salary: $150k-$250k</p>",
                "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
            },
            "form": [
                {
                    "title": "",
                    "questions": [
                        {"label": "Resume/CV", "field_types": ["input[file]"],
                         "required": True, "options": [], "description": ""},
                        {"label": "Sponsorship?", "field_types": ["multiple-choice"],
                         "required": True, "options": ["Yes", "No"], "description": ""},
                    ],
                },
                {
                    "title": "eeo",
                    "questions": [
                        {"label": "Gender", "field_types": ["dropdown", "select"],
                         "required": False, "options": ["Male", "Female"], "description": ""},
                    ],
                },
            ],
        }
        md = render_lever_job(data, source_url="https://jobs.lever.co/acme/abc-123")
        # Title (no company in source — just title).
        assert md.startswith("# Staff Product Designer")
        # Raw field names preserved.
        assert "**workplaceType**: remote" in md  # NOT normalized to "Remote"
        assert "**country**: US" in md
        # Categories rendered raw as dict JSON or with name joiner.
        # Description
        assert "Lead design." in md
        # Raw list title preserved as-is.
        assert "Responsibilities:" in md
        assert "Design" in md and "Ship" in md
        # Additional content
        assert "$150k-$250k" in md
        # Form sections with raw titles.
        assert "### eeo" in md  # Lever's class-derived section name
        # Every question present with its raw field-type labels.
        assert "**Resume/CV**" in md and "input[file]" in md
        assert "**Sponsorship?**" in md and "multiple-choice" in md
        assert "Options: Yes, No" in md
        assert "**Gender**" in md and "Options: Male, Female" in md
        # Source URL
        assert "**sourceUrl**: https://jobs.lever.co/acme/abc-123" in md
