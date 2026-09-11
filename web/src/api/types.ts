export type ApiErrorCode =
  | 'AUTHENTICATION_REQUIRED'
  | 'EMAIL_VERIFICATION_REQUIRED'
  | 'CSRF_FAILED'
  | 'INVALID_CREDENTIALS'
  | 'INVALID_REQUEST'
  | 'LOGIN_NOT_REQUIRED'
  | 'LOGOUT_NOT_AVAILABLE'
  | 'PERMISSION_DENIED'
  | 'NOT_FOUND'
  | 'METHOD_NOT_ALLOWED'
  | 'INTERNAL_ERROR'
  | 'LLM_RUNTIME_UNAVAILABLE'
  | 'RAG_CAPACITY_EXCEEDED'
  | 'SEARCH_FAILED'
  | 'API_ERROR'

export interface ApiError {
  code: ApiErrorCode | (string & {})
  message: string
  details: Record<string, unknown>
}

export interface ApiErrorResponse {
  ok: false
  error: ApiError
}

export interface SessionUser {
  email: string
  display_name: string
  email_verified: boolean
  is_staff: boolean
}

export interface SessionBootstrapResponse {
  ok: true
  auth: {
    mode: 'local' | 'required'
    login_required: boolean
    authenticated: boolean
  }
  user: SessionUser | null
}

export type ApiResponse<T> = T | ApiErrorResponse
