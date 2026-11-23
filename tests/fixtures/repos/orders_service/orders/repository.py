"""Raw SQL used by reports."""

TOTALS_SQL = "SELECT sku, sum(total_cents) FROM orders GROUP BY sku"
INSERT_SQL = "INSERT INTO orders (sku, quantity, total_cents) VALUES (:sku, :quantity, :total)"
