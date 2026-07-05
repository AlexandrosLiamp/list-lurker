import http.server
import socketserver
import threading
import time
import sys
sys.stdout.reconfigure(encoding='utf-8')
from playwright.sync_api import sync_playwright

PORT = 8085
URL = f"http://localhost:{PORT}/dashboard.html"

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("", PORT), QuietHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server

def run_tests():
    print("Starting local HTTP server...")
    server = start_server()
    time.sleep(0.5)

    print("Launching Chromium with console log monitoring...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # We want to capture console errors and page crashes
        console_errors = []
        console_logs = []
        
        context = browser.new_context()
        page = context.new_page()
        
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else console_logs.append(msg.text))
        page.on("pageerror", lambda err: console_errors.append(f"JS Exception: {err.message}"))
        page.on("crash", lambda: console_errors.append("Page crashed!"))

        print(f"Navigating to {URL}...")
        page.goto(URL)

        # Wait for data to load
        print("Waiting for tables to load...")
        try:
            page.wait_for_selector("#r-tbody tr", state="attached", timeout=10000)
            page.wait_for_selector("#g-tbody tr", state="attached", timeout=10000)
        except Exception as e:
            print(f"Error: Timeout loading tables. {e}")
            sys.exit(1)

        print("Switching to 'ram' tab...")
        page.evaluate("switchTab('ram')")
        time.sleep(0.5)

        print("\n--- 1. Table Headers & Columns Verification ---")
        # Check RAM table headers
        headers = page.locator("#r-table thead th").all_inner_texts()
        headers_cleaned = [h.replace("↕", "").strip() for h in headers]
        print(f"RAM Table Headers found: {headers_cleaned}")
        
        expected_headers = ["PRODUCT", "GEN/SPEED", "CAPACITY", "SOURCE", "PRICE", "GB", "DEAL QUALITY", "LINK"]
        for exp in expected_headers:
            assert any(exp in h.upper() for h in headers_cleaned), f"Expected header '{exp}' not found in {headers_cleaned}"
        print("RAM table headers are correct.")

        # Check first row's columns data
        first_row = page.locator("#r-tbody tr").first
        cells = first_row.locator("td")
        
        print("\nFirst RAM listing row cells:")
        name_text = cells.nth(0).inner_text()
        gen_speed_text = cells.nth(1).inner_text()
        capacity_text = cells.nth(2).inner_text()
        source_text = cells.nth(3).inner_text()
        price_text = cells.nth(4).inner_text()
        egb_text = cells.nth(5).inner_text()
        deal_quality_text = cells.nth(6).inner_text()
        link_text = cells.nth(7).inner_text()
        
        print(f"  - Product Name: {name_text}")
        print(f"  - Gen/Speed: {gen_speed_text}")
        print(f"  - Capacity: {capacity_text}")
        print(f"  - Source: {source_text}")
        print(f"  - Price: {price_text}")
        print(f"  - €/GB: {egb_text.splitlines()[0] if egb_text else ''}")
        print(f"  - Deal Quality: {deal_quality_text}")
        print(f"  - Link: {link_text}")

        # Assert data exists in the columns
        assert gen_speed_text != "", "Gen/Speed column is empty"
        assert capacity_text != "", "Capacity column is empty"
        assert egb_text != "", "€/GB column is empty"
        assert deal_quality_text != "", "Deal Quality column is empty"

        # Check for correct HTML structure in €/GB (should have ppr-bar-wrap and ppr-bar)
        egb_html = cells.nth(5).inner_html()
        assert "ppr-bar-wrap" in egb_html, "€/GB cell is missing ppr-bar-wrap class"
        assert "ppr-bar" in egb_html, "€/GB cell is missing ppr-bar class"
        print("Correct HTML structure for €/GB visual progress bar verified.")

        # Check for correct HTML structure in Deal Quality (should render a badge span or a dash)
        dq_html = cells.nth(6).inner_html()
        assert "badge" in dq_html or "–" in dq_html, "Deal Quality cell should contain a badge or an empty dash"
        print("Correct HTML structure for Deal Quality badge verified.")

        print("\n--- 2. Blacklist Button Functionality Verification ---")
        # Get details of the listing to blacklist (use URL if available, else name)
        blacklist_url = first_row.locator("button.r-hide-row").get_attribute("data-url")
        blacklist_name = first_row.locator("button.r-hide-row").get_attribute("data-name")
        blacklist_price = first_row.locator("button.r-hide-row").get_attribute("data-price")
        
        print(f"Blacklisting listing: {blacklist_name} ({blacklist_price} €) - URL: {blacklist_url}")

        initial_count = page.evaluate("ramRows.length")
        print(f"Initial RAM rows count: {initial_count}")

        # Click blacklist button on the first row
        first_row.locator("button.r-hide-row").click()
        
        # Wait for dynamically updated counts
        time.sleep(0.5)
        after_click_count = page.evaluate("ramRows.length")
        print(f"RAM rows count after blacklisting: {after_click_count}")
        assert after_click_count == initial_count - 1, "The listing was not dynamically removed from RAM rows"
        
        # Verify it's no longer the first row
        new_first_name = page.locator("#r-tbody tr").first.locator("td").nth(0).inner_text()
        print(f"New first row product name: {new_first_name}")
        assert new_first_name != blacklist_name or after_click_count == 0, "Blacklisted listing is still showing in the table"
        print("Listing successfully filtered out of the active list dynamically.")

        # Reload page to test persistence of blacklist in localStorage
        print("Reloading page to test blacklist persistence...")
        page.reload()
        page.wait_for_selector("#r-tbody tr", state="attached", timeout=10000)
        
        reload_count = page.evaluate("ramRows.length")
        print(f"RAM rows count after reload: {reload_count}")
        assert reload_count == initial_count - 1, "Blacklist did not persist in localStorage after page reload"
        
        reload_first_name = page.locator("#r-tbody tr").first.locator("td").nth(0).inner_text()
        assert reload_first_name != blacklist_name or reload_count == 0, "Blacklisted listing is visible after page reload"
        print("Blacklist persistence verified (listing is still filtered out after reload).")

        # Restore / Clear Blacklist
        print("Cleaning up blacklist to restore state...")
        page.evaluate("localStorage.removeItem('global_blacklist')")
        page.reload()
        page.wait_for_selector("#r-tbody tr", state="attached", timeout=10000)
        
        final_count = page.evaluate("ramRows.length")
        print(f"RAM rows count after clearing blacklist and reload: {final_count}")
        assert final_count == initial_count, "Failed to restore rows after clearing blacklist"
        print("Cleanup successful, state restored.")

        print("\n--- 3. Console Errors & Page Performance verification ---")
        if console_errors:
            print(f"Warning: {len(console_errors)} errors detected in browser console:")
            for err in console_errors:
                print(f"  - {err}")
            # Ensure none of these console errors are critical script crashes
            critical_errors = [e for e in console_errors if "SyntaxError" in e or "ReferenceError" in e or "TypeError" in e]
            assert len(critical_errors) == 0, f"Critical script crashes detected: {critical_errors}"
        else:
            print("Zero console errors or script crashes detected.")

        print("\nComprehensive dashboard details and blacklist checks PASSED successfully.")
        browser.close()

    server.shutdown()
    print("Server stopped.")

if __name__ == "__main__":
    run_tests()
