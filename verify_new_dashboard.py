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

    def do_GET(self):
        if self.path.endswith("/api/state"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"negotiations": [], "pending": [], "jobs": [], "targets": []}')
            return
        elif self.path.endswith("/api/reports"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"urls": []}')
            return
        super().do_GET()

def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("", PORT), QuietHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server

def run_tests():
    print("Starting local HTTP server...")
    server = start_server()
    # Give the server a moment to start
    time.sleep(0.5)

    print("Launching Chromium...")
    with sync_playwright() as p:
        # Launch headless browser
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Listen to console errors
        console_errors = []
        page.on("console", lambda msg: console_errors.append(f"Console {msg.type.upper()}: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(f"Page Error: {err.message}"))
        page.on("requestfailed", lambda req: console_errors.append(f"Request Failed: {req.url} - {req.failure}"))
        page.on("response", lambda res: console_errors.append(f"HTTP Error {res.status}: {res.url}") if res.status >= 400 else None)

        print(f"Navigating to {URL}...")
        page.goto(URL)

        # Wait for tables to load (indicated by tbody rows rendering or status indicators turning teal)
        print("Waiting for data to load...")
        try:
            page.wait_for_selector("#r-tbody tr", state="attached", timeout=10000)
            page.wait_for_selector("#g-tbody tr", state="attached", timeout=10000)
        except Exception as e:
            print(f"Error: Timeout waiting for data rows to render. {e}")
            sys.exit(1)

        print("\n--- 1. Snapshot States Verification ---")
        ram_snap = page.evaluate("ramSnap")
        gpu_snap = page.evaluate("gpuSnap")
        print(f"Global JS ramSnap: {ram_snap} (Expected: '__all__')")
        print(f"Global JS gpuSnap: {gpu_snap} (Expected: '__all__')")
        
        # Verify active class on 'All' snapshot tab
        ram_all_active = page.eval_on_selector("#r-ts-tabs .ts-tab.all", "el => el.classList.contains('active')")
        gpu_all_active = page.eval_on_selector("#g-ts-tabs .ts-tab.all", "el => el.classList.contains('active')")
        print(f"RAM 'All' Tab is active class: {ram_all_active} (Expected: True)")
        print(f"GPU 'All' Tab is active class: {gpu_all_active} (Expected: True)")

        assert ram_snap == "__all__", "RAM snapshot should default to '__all__'"
        assert gpu_snap == "__all__", "GPU snapshot should default to '__all__'"
        assert ram_all_active, "RAM 'All' tab should have active class"
        assert gpu_all_active, "GPU 'All' tab should have active class"

        print("\n--- 2. Loaded Data Counts Verification ---")
        ram_pill_text = page.locator("#pill-ram").inner_text()
        gpu_pill_text = page.locator("#pill-gpu").inner_text()
        retail_pill_text = page.locator("#pill-retail").inner_text()
        laptop_pill_text = page.locator("#pill-laptops").inner_text()
        
        print(f"RAM Pill Text: {ram_pill_text}")
        print(f"GPU Pill Text: {gpu_pill_text}")
        print(f"Retail Pill Text: {retail_pill_text}")
        print(f"Laptop Pill Text: {laptop_pill_text}")

        # Also get in-memory sizes
        ram_rows_cnt = page.evaluate("ramRows.length")
        gpu_rows_cnt = page.evaluate("gpuRows.length")
        cpu_rows_cnt = page.evaluate("cpuRows.length")
        mobo_rows_cnt = page.evaluate("moboRows.length")
        retail_rows_cnt = page.evaluate("retailRows.length")
        laptop_rows_cnt = page.evaluate("laptopRows.length")

        print(f"In-memory RAM rows: {ram_rows_cnt}")
        print(f"In-memory GPU rows: {gpu_rows_cnt}")
        print(f"In-memory CPU rows: {cpu_rows_cnt}")
        print(f"In-memory Motherboard rows: {mobo_rows_cnt}")
        print(f"In-memory Retail GPU rows: {retail_rows_cnt}")
        print(f"In-memory Laptop rows: {laptop_rows_cnt}")

        assert ram_rows_cnt > 0, "No RAM rows loaded in memory"
        assert gpu_rows_cnt > 0, "No GPU rows loaded in memory"
        assert cpu_rows_cnt > 0, "No CPU rows loaded in memory"
        assert mobo_rows_cnt > 0, "No Motherboard rows loaded in memory"
        assert retail_rows_cnt > 0, "No Retail GPU rows loaded in memory"
        assert laptop_rows_cnt > 0, "No Laptop rows loaded in memory"

        print("\n--- 3. X-Axis Limits Verification ---")
        print("Switching to 'gmatrix' tab...")
        page.evaluate("switchTab('gmatrix')")
        time.sleep(0.5)
        max_x = page.evaluate("gMatrixChart ? gMatrixChart.options.scales.x.max : null")
        print(f"GPU Matrix Chart X-axis Max: {max_x} (Expected: 800)")
        assert max_x == 800, "X-axis max limit should be 800"

        print("Switching to 'rmatrix' tab...")
        page.evaluate("switchTab('rmatrix')")
        time.sleep(0.5)
        max_x_ram = page.evaluate("rMatrixChart ? rMatrixChart.options.scales.x.max : null")
        print(f"RAM Matrix Chart X-axis Max: {max_x_ram} (Expected: 250)")
        assert max_x_ram == 250, "RAM X-axis max limit should be 250"

        print("Switching to 'laptops' tab...")
        page.evaluate("switchTab('laptops')")
        time.sleep(0.5)
        lp_chart_exists = page.evaluate("lpChart !== null")
        print(f"Laptop Chart exists: {lp_chart_exists}")
        assert lp_chart_exists, "Laptop chart should be initialized on tab render"

        print("\n--- 4. Laptops Tab Filters Verification ---")
        # 1. Total laptops count
        total_lps = page.evaluate("laptopRows.length")
        print(f"Total laptops in memory: {total_lps}")

        # 2. Filter by Name (e.g. "Victus")
        print("Applying Name/Model filter: 'Victus'")
        page.evaluate("document.getElementById('lp-filter-name').value = 'Victus'; lpApplyFilters(); renderLaptopTable();")
        filtered_lps_victus = page.evaluate("lpFiltered.length")
        print(f"Laptops count after 'Victus' filter: {filtered_lps_victus}")
        assert filtered_lps_victus < total_lps, "Filtering by name should reduce count"
        
        all_match_victus = page.evaluate("lpFiltered.every(r => r.name.toLowerCase().includes('victus') || (r.cpu_name||'').toLowerCase().includes('victus') || (r.gpu_name||'').toLowerCase().includes('victus'))")
        print(f"All filtered rows match 'Victus' query: {all_match_victus}")
        assert all_match_victus, "Filtered rows do not match the query"

        # 3. Clear Name filter, Apply Price filter (max 600)
        print("Clearing Name filter and applying Max Price filter: '600'")
        page.evaluate("document.getElementById('lp-filter-name').value = ''; document.getElementById('lp-filter-max-price').value = '600'; lpApplyFilters(); renderLaptopTable();")
        filtered_lps_price = page.evaluate("lpFiltered.length")
        print(f"Laptops count after max price filter: {filtered_lps_price}")
        assert filtered_lps_price < total_lps, "Filtering by price should reduce count"
        
        # Check rendered prices in table
        rendered_prices = page.eval_on_selector_all("#lp-tbody tr td.price", "cells => cells.map(c => c.textContent)")
        print(f"Sample rendered prices: {rendered_prices[:5]}")
        all_prices_under_600 = True
        for p_str in rendered_prices:
            if p_str != '–':
                val = float(p_str.replace('€', '').replace(',', '').strip())
                if val > 600:
                    all_prices_under_600 = False
        print(f"All rendered prices are <= 600: {all_prices_under_600}")
        assert all_prices_under_600, "Some rendered prices exceed 600 €"

        # 4. Clear filters
        print("Clearing Max Price filter")
        page.evaluate("document.getElementById('lp-filter-max-price').value = ''; lpApplyFilters(); renderLaptopTable();")

        print("\n--- 5. Negotiations (Skoop offers) tab ---")
        page.evaluate("switchTab('offers')")
        # NEGOTIATIONS loads async from negotiations.json; wait for it (ledger has ≥1 offer).
        try:
            page.wait_for_function(
                "typeof NEGOTIATIONS !== 'undefined' && NEGOTIATIONS.length >= 1", timeout=5000)
        except Exception:
            print("Warning: negotiations.json empty/unavailable — skipping content assert.")
        neg_count = page.evaluate("typeof NEGOTIATIONS !== 'undefined' ? NEGOTIATIONS.length : -1")
        tab_active = page.eval_on_selector("#tab-offers", "el => el.classList.contains('active')")
        offers_html = page.locator("#offers-list").inner_html()
        print(f"NEGOTIATIONS loaded: {neg_count}")
        print(f"Offers tab active: {tab_active}")
        assert tab_active, "offers tab content should be active after switchTab('offers')"
        if neg_count >= 1:
            assert "offer-card" in offers_html, "offers-list should render at least one offer-card"
            print("Offers tab renders offer cards OK.")

        if console_errors:
            print("\nWarnings/Errors found during page load:")
            for err in console_errors:
                print(f" - {err}")
        else:
            print("\nNo console errors found.")

        print("\nBasic dashboard functionality checks PASSED successfully.")
        browser.close()
    
    server.shutdown()
    print("Server stopped.")

if __name__ == "__main__":
    run_tests()
