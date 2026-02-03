#!/usr/bin/env python3
"""
Playwright tests for Zulip portal.

Tests that the Zulip portal is working correctly:
1. Login page loads
2. Admin can log in
3. App loads after login (no infinite spinner)
4. Static assets load correctly

Usage:
    python scripts/test_zulip_portal.py
    python scripts/test_zulip_portal.py --headed  # Show browser
    python scripts/test_zulip_portal.py --url https://zulip.example.com

Prerequisites:
    pip install playwright
    playwright install chromium
"""

import argparse
import sys
import time
from dataclasses import dataclass


@dataclass
class TestResult:
    name: str
    passed: bool
    message: str
    duration: float = 0.0


def test_zulip_portal(
    base_url: str = "https://localhost:8443",
    admin_email: str = "admin@agent-economy.local",
    admin_password: str = "admin-dev-password-123",
    headed: bool = False,
    screenshots_dir: str = "/tmp",
) -> list[TestResult]:
    """Run all Zulip portal tests."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(ignore_https_errors=True)

        # Track failed requests
        failed_requests = []
        page.on('requestfailed', lambda req: failed_requests.append({
            'url': req.url,
            'error': req.failure
        }))

        # Test 1: Login page loads
        start = time.time()
        try:
            response = page.goto(f"{base_url}/login/", timeout=30000)
            duration = time.time() - start

            if response.status == 200 and "Log in" in page.title():
                results.append(TestResult("Login page loads", True, f"Loaded in {duration:.1f}s", duration))
            else:
                results.append(TestResult("Login page loads", False, f"Status: {response.status}, Title: {page.title()}"))
                page.screenshot(path=f"{screenshots_dir}/test-login-fail.png")
        except Exception as e:
            results.append(TestResult("Login page loads", False, str(e)))
            browser.close()
            return results

        # Test 2: Can fill login form
        try:
            page.fill('input[name="username"]', admin_email)
            page.fill('input[name="password"]', admin_password)
            results.append(TestResult("Login form fillable", True, "Filled credentials"))
        except Exception as e:
            results.append(TestResult("Login form fillable", False, str(e)))
            browser.close()
            return results

        # Test 3: Login works
        start = time.time()
        try:
            page.click('button[type="submit"]')
            # Wait for navigation away from login page
            page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
            duration = time.time() - start

            if "/login" not in page.url:
                results.append(TestResult("Login succeeds", True, f"Redirected to {page.url}", duration))
            else:
                results.append(TestResult("Login succeeds", False, "Still on login page"))
                page.screenshot(path=f"{screenshots_dir}/test-login-stuck.png")
        except Exception as e:
            results.append(TestResult("Login succeeds", False, str(e)))
            page.screenshot(path=f"{screenshots_dir}/test-login-error.png")
            browser.close()
            return results

        # Test 4: App loads (not stuck on spinner)
        start = time.time()
        try:
            # Wait for the app to load - look for sidebar or channels
            time.sleep(5)  # Give JS time to execute

            # Check for loading indicator still present
            loading = page.query_selector('.app-loading')
            if loading and loading.is_visible():
                # Still loading after 5s - wait more
                time.sleep(5)
                loading = page.query_selector('.app-loading')

            duration = time.time() - start

            # Check for app content
            body_text = page.inner_text('body')
            has_channels = "CHANNELS" in body_text or "job-board" in body_text
            has_sidebar = page.query_selector('.left-sidebar, #left-sidebar') is not None

            if has_channels or has_sidebar:
                results.append(TestResult("App UI loads", True, f"Loaded in {duration:.1f}s", duration))
            else:
                results.append(TestResult("App UI loads", False, "Sidebar/channels not found"))
                page.screenshot(path=f"{screenshots_dir}/test-app-fail.png")
        except Exception as e:
            results.append(TestResult("App UI loads", False, str(e)))
            page.screenshot(path=f"{screenshots_dir}/test-app-error.png")

        # Test 5: Static assets loaded
        # Filter to only Zulip static assets, ignore external CDN
        zulip_failures = [r for r in failed_requests if base_url.split("://")[1].split(":")[0] in r['url']]

        if len(zulip_failures) == 0:
            results.append(TestResult("Static assets load", True, f"All assets loaded ({len(failed_requests)} external failures ignored)"))
        else:
            results.append(TestResult("Static assets load", False, f"{len(zulip_failures)} failed: {zulip_failures[0]['url']}"))

        # Final screenshot
        page.screenshot(path=f"{screenshots_dir}/zulip-final.png")

        browser.close()

    return results


def main():
    parser = argparse.ArgumentParser(description="Test Zulip portal")
    parser.add_argument("--url", default="https://localhost:8443", help="Zulip base URL")
    parser.add_argument("--email", default="admin@agent-economy.local", help="Admin email")
    parser.add_argument("--password", default="admin-dev-password-123", help="Admin password")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--screenshots", default="/tmp", help="Screenshots directory")

    args = parser.parse_args()

    print(f"Testing Zulip portal at {args.url}\n")

    results = test_zulip_portal(
        base_url=args.url,
        admin_email=args.email,
        admin_password=args.password,
        headed=args.headed,
        screenshots_dir=args.screenshots,
    )

    # Print results
    passed = 0
    failed = 0

    for r in results:
        status = "✓" if r.passed else "✗"
        print(f"  {status} {r.name}: {r.message}")
        if r.passed:
            passed += 1
        else:
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")

    if failed > 0:
        print(f"\nScreenshots saved to {args.screenshots}/")
        sys.exit(1)
    else:
        print("\nAll tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
