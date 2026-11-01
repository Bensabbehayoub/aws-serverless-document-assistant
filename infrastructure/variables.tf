variable "project" {
  description = "Short project name, used as a prefix for every resource."
  type        = string
  default     = "docassist"
}

variable "environment" {
  description = "Environment name, e.g. dev or prod."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "eu-north-1"
}

variable "frontend_origin" {
  description = "Where the frontend runs. Used for CORS and the Cognito login/logout URLs."
  type        = string
  default     = "http://localhost:3000"
}

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications. Leave empty to skip email alerts."
  type        = string
  default     = ""
}

variable "ai_provider" {
  description = "How questions are answered: extractive (no AI, free), gemini or bedrock."
  type        = string
  default     = "extractive"

  validation {
    condition     = contains(["extractive", "gemini", "bedrock"], var.ai_provider)
    error_message = "ai_provider must be extractive, gemini or bedrock."
  }
}

variable "gemini_key_parameter" {
  description = "SSM Parameter Store name holding the Gemini API key (only used when ai_provider = gemini)."
  type        = string
  default     = "/docassist/gemini-api-key"
}

variable "bedrock_model_id" {
  description = "Bedrock model or inference profile (only used when ai_provider = bedrock)."
  type        = string
  default     = "eu.amazon.nova-micro-v1:0"
}

variable "api_throttle_rate" {
  description = "Average API requests per second allowed."
  type        = number
  default     = 10
}

variable "api_throttle_burst" {
  description = "Short burst of API requests allowed."
  type        = number
  default     = 20
}

variable "log_retention_days" {
  description = "How long CloudWatch keeps Lambda and API logs."
  type        = number
  default     = 30
}

variable "allow_self_signup" {
  description = "Let anyone create an account on the login page. Keep false for a private demo."
  type        = bool
  default     = false
}
