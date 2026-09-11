import { apiRequest } from './http'

export type WorkspaceKind = 'personal' | 'team'
export type WorkspaceRole = 'admin' | 'member'
export type WorkspaceMemberStatus = 'active' | 'pending'
export type WorkspaceInvitationStatus = 'pending' | 'accepted' | 'declined' | 'revoked'

export interface WorkspaceSummary {
  uid: string
  name: string
  kind: WorkspaceKind
  role?: WorkspaceRole
}

export interface WorkspaceMember {
  user_id: number
  email: string
  display_name: string
  role: WorkspaceRole
  status: WorkspaceMemberStatus
  joined_at: string
}

export interface WorkspaceInvitation {
  id: number
  workspace: WorkspaceSummary
  invited_by: string | null
  status: WorkspaceInvitationStatus
  created_at: string
}

interface WorkspaceListResponse {
  ok: true
  workspaces: WorkspaceSummary[]
  active_workspace_uid: string
}

interface WorkspaceResponse {
  ok: true
  workspace: WorkspaceSummary
}

interface MemberListResponse {
  ok: true
  members: WorkspaceMember[]
}

interface MemberResponse {
  ok: true
  member: WorkspaceMember
}

interface InviteCodeIssueResponse {
  ok: true
  code: string
  max_uses: number | null
  expires_at: string | null
}

interface InvitationResponse {
  ok: true
  invitation: WorkspaceInvitation
}

interface InvitationListResponse {
  ok: true
  invitations: WorkspaceInvitation[]
}

interface InvitationRespondResponse {
  ok: true
  invitation: WorkspaceInvitation
  workspace: WorkspaceSummary | null
}

const BASE = '/api/workspaces/v1'

export const tenantApi = {
  async listWorkspaces(): Promise<WorkspaceListResponse> {
    return apiRequest<WorkspaceListResponse>(`${BASE}/`)
  },

  async createWorkspace(name: string, kind: WorkspaceKind): Promise<WorkspaceSummary> {
    const payload = await apiRequest<WorkspaceResponse>(`${BASE}/`, {
      method: 'POST',
      json: { name, kind },
    })
    return payload.workspace
  },

  async currentWorkspace(): Promise<WorkspaceSummary> {
    const payload = await apiRequest<WorkspaceResponse>(`${BASE}/current/`)
    return payload.workspace
  },

  async switchWorkspace(workspaceUid: string): Promise<WorkspaceSummary> {
    const payload = await apiRequest<WorkspaceResponse>(`${BASE}/switch/`, {
      method: 'POST',
      json: { workspace_uid: workspaceUid },
    })
    return payload.workspace
  },

  async listMembers(): Promise<WorkspaceMember[]> {
    const payload = await apiRequest<MemberListResponse>(`${BASE}/current/members/`)
    return payload.members
  },

  async changeMemberRole(userId: number, role: WorkspaceRole): Promise<WorkspaceMember> {
    const payload = await apiRequest<MemberResponse>(`${BASE}/current/members/${userId}/`, {
      method: 'PATCH',
      json: { role },
    })
    return payload.member
  },

  async removeMember(userId: number): Promise<void> {
    await apiRequest<{ ok: true }>(`${BASE}/current/members/${userId}/`, {
      method: 'DELETE',
    })
  },

  async issueInviteCode(maxUses?: number): Promise<InviteCodeIssueResponse> {
    return apiRequest<InviteCodeIssueResponse>(`${BASE}/current/invite-code/`, {
      method: 'POST',
      json: maxUses ? { max_uses: maxUses } : {},
    })
  },

  async redeemInviteCode(code: string): Promise<WorkspaceSummary> {
    const payload = await apiRequest<WorkspaceResponse>(`${BASE}/invite-code/redeem/`, {
      method: 'POST',
      json: { code },
    })
    return payload.workspace
  },

  async createInvite(email: string): Promise<WorkspaceInvitation> {
    const payload = await apiRequest<InvitationResponse>(`${BASE}/current/invites/`, {
      method: 'POST',
      json: { email },
    })
    return payload.invitation
  },

  async listInviteInbox(): Promise<WorkspaceInvitation[]> {
    const payload = await apiRequest<InvitationListResponse>(`${BASE}/invites/inbox/`)
    return payload.invitations
  },

  async acceptInvite(invitationId: number): Promise<InvitationRespondResponse> {
    return apiRequest<InvitationRespondResponse>(`${BASE}/invites/${invitationId}/accept/`, {
      method: 'POST',
    })
  },

  async declineInvite(invitationId: number): Promise<InvitationRespondResponse> {
    return apiRequest<InvitationRespondResponse>(`${BASE}/invites/${invitationId}/decline/`, {
      method: 'POST',
    })
  },
}
