import { ORDERS_URL } from "./config";

export interface OrderTotal {
  total_cents: number;
}

export async function placeOrder(sku: string, quantity: number, price: string): Promise<OrderTotal> {
  const resp = await fetch(`${ORDERS_URL}/orders?sku=${sku}&quantity=${quantity}&price=${price}`, { method: "POST" });
  return (await resp.json()) as OrderTotal;
}

export async function getOrder(orderId: number): Promise<unknown> {
  const resp = await fetch(`${ORDERS_URL}/orders/${orderId}`);
  return resp.json();
}
