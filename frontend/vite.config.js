import { defineConfig } from "vite";

// Port 3000 must match frontend_origin in Terraform (CORS and Cognito callback URLs).
export default defineConfig({
  server: { port: 3000, strictPort: true },
  preview: { port: 3000, strictPort: true }
});
