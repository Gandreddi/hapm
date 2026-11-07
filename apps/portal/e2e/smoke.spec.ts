import { expect, test, type Page } from "@playwright/test";

// Dev-only test identities from services/oidc_stub/users.yaml.
const ORCHESTRATOR = process.env.ORCHESTRATOR_URL ?? "http://127.0.0.1:8002";
const OIDC = process.env.OIDC_URL ?? "http://127.0.0.1:8006";

async function token(user: string, password: string): Promise<string> {
  const resp = await fetch(`${OIDC}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "password", username: user, password }),
  });
  return ((await resp.json()) as { access_token: string }).access_token;
}

async function login(page: Page, user: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(user);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("who")).toContainText(user);
}

test("unauthenticated users are sent to sign in, bad passwords are refused", async ({ page }) => {
  await page.goto("/runs");
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Username").fill("viewer");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Sign-in failed")).toBeVisible();
});

test("viewer sees runs but no approval inbox", async ({ page }) => {
  await login(page, "viewer", "viewer-dev");
  await expect(page.getByTestId("runs-table")).toBeVisible();
  await expect(page.getByRole("link", { name: "Approval inbox" })).toHaveCount(0);
  await page.goto("/inbox");
  await expect(page.getByText("needs the approver role")).toBeVisible();
});

test("approver approves from the inbox and the timeline shows the whole run", async ({ page }) => {
  const title = `Portal story ${Date.now()}`;
  const requester = await token("requester", "requester-dev");
  const submitted = await fetch(`${ORCHESTRATOR}/v1/tasks`, {
    method: "POST",
    headers: { Authorization: `Bearer ${requester}`, "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: "dummy_echo", input: { text: "Playwright smoke test of the approval inbox.", title } }),
  });
  expect(submitted.ok).toBeTruthy();
  const { run_id } = (await submitted.json()) as { run_id: string };

  await login(page, "approver", "approver-dev");
  await expect(async () => {
    await page.goto(`/runs/${run_id}`);
    await expect(page.getByTestId("run-state")).toHaveText("waiting_approval", { timeout: 2000 });
  }).toPass({ timeout: 60_000 });
  await expect(page.getByTestId("waiting-approval")).toContainText("create_story");

  await page.goto("/inbox");
  await page.locator(`tr[data-run="${run_id}"] a`).click();
  await expect(page.getByTestId("preview")).toContainText("title:");
  await page.getByLabel("Comment").fill("approved by playwright");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByTestId("approval-status")).toHaveText("approved");

  await expect(async () => {
    await page.goto(`/runs/${run_id}`);
    await expect(page.getByTestId("run-state")).toHaveText("succeeded", { timeout: 2000 });
  }).toPass({ timeout: 60_000 });
  const timeline = page.getByTestId("timeline");
  for (const kind of ["llm", "tool", "approval", "policy", "memory", "step", "state"]) {
    await expect(timeline.locator(`tr[data-kind="${kind}"]`).first()).toBeVisible();
  }
});
