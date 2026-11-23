import { placeOrder } from "./ordersApi";

export class Checkout {
  async submit(sku: string): Promise<number> {
    const result = await placeOrder(sku, 1, "9.99");
    return result.total_cents;
  }
}
