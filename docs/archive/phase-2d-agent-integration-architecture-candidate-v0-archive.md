# GovernLoop Phase 2D Agent Integration Architecture Candidate v0

Status: ARCHIVED

Date: 2026-08-23

## Purpose

This document records the Phase 2D architecture research outcome after Phase 2C acceptance.

This is an architecture candidate only.

It does NOT authorize implementation.

## Baseline

Completed:

- Phase 2A Installer
- Phase 2B Runtime Bundle
- Phase 2C Stable CLI + Doctor
- Qclaw Cold User Acceptance

Current direction:

Agent
→ GovernLoop CLI
→ Installed Runtime
→ Session Manager
→ Neutral Relay
→ Reviewer

## Research Conclusions

### Agent Discovery

Recommended baseline:

- stable CLI discovery through `~/.governloop/bin/governloop`
- installed universal skill as capability contract
- avoid PATH mutation as a governance dependency

### First-run Onboarding

Recommended flow:

install (user confirmation)
→ doctor
→ new
→ bind reviewer conversation
→ checkpoints
→ end

User confirmation remains required for installation and external conversation binding.

### Adapter Boundary

Recommended baseline:

Option A: agents call GovernLoop CLI directly.

Thin adapters may exist later only as UX wrappers.

Deep agent-specific integrations are not recommended as the protocol foundation.

### Universal Protocol

Candidate shared contracts:

- session contract
- checkpoint contract
- reviewer binding contract
- task lifecycle contract

Further formalization requires separate adoption decision.

### Security Boundary

GovernLoop remains a governance and transport layer.

It should not become:

- authority owner
- credential owner
- merge/deploy authority
- browser automation controller

## Risks Recorded

- relay early stop / truncated reviewer responses
- session identity normalization
- future capability discovery

## Governance Boundary

This archive does NOT authorize:

- Phase 2D implementation
- agent adapters
- installer changes
- CLI changes
- PATH integration
- Chrome/CDP integration

Implementation requires Architecture Review and Product Owner approval.

## Final Status

ARCHIVED

Next step: future Architecture Review and Adoption Decision only.
