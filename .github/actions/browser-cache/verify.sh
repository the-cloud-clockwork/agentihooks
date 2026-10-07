#!/usr/bin/env bash
set -euo pipefail

python -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.set_content("<title>Chromium ready</title>")
    assert page.title() == "Chromium ready"
    browser.close()'
