#!/usr/bin/env python

import os
from dotenv import load_dotenv
from jira import JIRA
from fastmcp import FastMCP
from fastapi import HTTPException
import json
from datetime import datetime, timedelta
from collections import defaultdict

# ─── 1. Load environment variables ─────────────────────────────────────────────
load_dotenv()

JIRA_URL       = os.getenv("JIRA_URL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

if not all([JIRA_URL, JIRA_API_TOKEN]):
    raise RuntimeError("Missing JIRA_URL or JIRA_API_TOKEN environment variables")

# ─── 2. Create a Jira client ───────────────────────────────────────────────────
#    Uses token_auth (API token) for authentication.
jira_client = JIRA(server=str(JIRA_URL), token_auth=str(JIRA_API_TOKEN))

# ─── 3. Instantiate the MCP server ─────────────────────────────────────────────
mcp = FastMCP("Jira Context Server")

# ─── 4. Register the get_jira tool ─────────────────────────────────────────────
@mcp.tool()
def get_jira(issue_key: str) -> str:
    """
    Fetch the Jira issue identified by 'issue_key' using jira_client,
    then return a Markdown string: "# ISSUE-KEY: summary\n\ndescription"
    """
    try:
        issue = jira_client.issue(issue_key)
    except Exception as e:
        # If the JIRA client raises an error (e.g. issue not found),
        # wrap it in an HTTPException so MCP/Client sees a 4xx/5xx.
        raise HTTPException(status_code=404, detail=f"Failed to fetch Jira issue {issue_key}: {e}")

    # Extract summary & description fields
    summary     = issue.fields.summary or ""
    description = issue.fields.description or ""

    return f"# {issue_key}: {summary}\n\n{description}"

def to_markdown(obj):
    if isinstance(obj, dict):
        return '```json\n' + json.dumps(obj, indent=2) + '\n```'
    elif hasattr(obj, 'raw'):
        return '```json\n' + json.dumps(obj.raw, indent=2) + '\n```'
    elif isinstance(obj, list):
        return '\n'.join([to_markdown(o) for o in obj])
    else:
        return str(obj)

@mcp.tool()
def search_issues(jql: str, max_results: int = 10) -> str:
    """Search issues using JQL."""
    try:
        issues = jira_client.search_issues(jql, maxResults=max_results)
        return to_markdown([i.raw for i in issues])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"JQL search failed: {e}")

@mcp.tool()
def get_issues_with_details(jql: str, max_results: int = 50) -> str:
    """Search issues using JQL and return detailed fields including story points, status, and assignee."""
    try:
        # Request specific fields that are useful for sprint analysis
        fields = 'summary,status,assignee,customfield_10016,priority,issuetype,created,updated,resolution,resolutiondate'
        issues = jira_client.search_issues(jql, maxResults=max_results, fields=fields, expand='changelog')
        
        result = []
        for issue in issues:
            issue_data = {
                'key': issue.key,
                'summary': issue.fields.summary,
                'status': issue.fields.status.name,
                'status_category': issue.fields.status.statusCategory.name,
                'assignee': issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned',
                'story_points': getattr(issue.fields, 'customfield_10016', None),  # Story Points field
                'priority': issue.fields.priority.name if issue.fields.priority else None,
                'issue_type': issue.fields.issuetype.name,
                'created': str(issue.fields.created),
                'updated': str(issue.fields.updated),
                'resolution': issue.fields.resolution.name if issue.fields.resolution else None,
                'resolution_date': str(issue.fields.resolutiondate) if issue.fields.resolutiondate else None
            }
            result.append(issue_data)
        
        return '```json\n' + json.dumps(result, indent=2) + '\n```'
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get detailed issues: {e}")

@mcp.tool()
def analyze_sprint_commitment(sprint_id: int) -> str:
    """Analyze sprint velocity and throughput using both story points and issue counts."""
    try:
        # Get sprint details
        sprint = jira_client.sprint(sprint_id)
        
        # Search for all issues in this sprint
        jql = f"sprint = {sprint_id}"
        fields = 'summary,status,customfield_10016,assignee,priority,issuetype'
        issues = jira_client.search_issues(jql, maxResults=200, fields=fields)
        
        # Calculate story point metrics
        total_committed_points = 0
        completed_points = 0
        in_progress_points = 0
        todo_points = 0
        
        # Calculate issue count metrics
        total_issues = len(issues)
        completed_issues = 0
        in_progress_issues = 0
        todo_issues = 0
        
        issue_breakdown = []
        issues_with_points = 0
        
        for issue in issues:
            story_points = getattr(issue.fields, 'customfield_10016', 0) or 0
            status_category = issue.fields.status.statusCategory.name
            
            issue_info = {
                'key': issue.key,
                'summary': issue.fields.summary,
                'story_points': story_points,
                'status': issue.fields.status.name,
                'status_category': status_category,
                'assignee': issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned'
            }
            issue_breakdown.append(issue_info)
            
            # Count issues by status
            if status_category in ['Done', 'Complete']:
                completed_issues += 1
                if story_points:
                    completed_points += story_points
            elif status_category in ['In Progress', 'In Development']:
                in_progress_issues += 1
                if story_points:
                    in_progress_points += story_points
            else:
                todo_issues += 1
                if story_points:
                    todo_points += story_points
            
            # Track story points
            if story_points:
                issues_with_points += 1
                total_committed_points += story_points
        
        # Calculate completion rates
        story_point_completion_rate = (completed_points / total_committed_points * 100) if total_committed_points > 0 else 0
        issue_completion_rate = (completed_issues / total_issues * 100) if total_issues > 0 else 0
        
        # Determine which metric to use for assessment
        uses_story_points = issues_with_points > 0
        primary_completion_rate = story_point_completion_rate if uses_story_points else issue_completion_rate
        
        # Assess overcommitment risk
        remaining_work = (total_committed_points - completed_points) if uses_story_points else (total_issues - completed_issues)
        work_in_progress = in_progress_points if uses_story_points else in_progress_issues
        work_remaining = remaining_work - work_in_progress
        
        # Determine overcommitment status
        if primary_completion_rate >= 80:
            commitment_status = 'ON_TRACK'
        elif primary_completion_rate >= 60:
            commitment_status = 'AT_RISK'
        elif primary_completion_rate >= 40:
            commitment_status = 'OVERCOMMITTED'
        else:
            commitment_status = 'SEVERELY_OVERCOMMITTED'
        
        analysis = {
            'sprint_info': {
                'id': sprint_id,
                'name': sprint.name,
                'state': sprint.state,
                'start_date': str(sprint.startDate) if hasattr(sprint, 'startDate') else None,
                'end_date': str(sprint.endDate) if hasattr(sprint, 'endDate') else None
            },
            'commitment_analysis': {
                'status': commitment_status,
                'is_overcommitted': commitment_status in ['OVERCOMMITTED', 'SEVERELY_OVERCOMMITTED'],
                'completion_rate': round(primary_completion_rate, 1),
                'remaining_work': remaining_work,
                'work_in_progress': work_in_progress,
                'work_not_started': work_remaining
            },
            'velocity_metrics': {
                'uses_story_points': uses_story_points,
                'story_points': {
                    'total_committed': total_committed_points,
                    'completed': completed_points,
                    'in_progress': in_progress_points,
                    'todo': todo_points,
                    'completion_rate': round(story_point_completion_rate, 1)
                },
                'issue_throughput': {
                    'total_committed': total_issues,
                    'completed': completed_issues,
                    'in_progress': in_progress_issues,
                    'todo': todo_issues,
                    'completion_rate': round(issue_completion_rate, 1)
                },
                'primary_metric': 'story_points' if uses_story_points else 'issue_count'
            },
            'sprint_health': {
                'completion_rate': round(primary_completion_rate, 1),
                'risk_level': 'LOW' if primary_completion_rate >= 70 else 'MEDIUM' if primary_completion_rate >= 50 else 'HIGH',
                'is_on_track': primary_completion_rate >= 70,
                'recommendations': []
            },
            'issue_breakdown': issue_breakdown
        }
        
        # Add recommendations based on analysis
        commitment_status = analysis['commitment_analysis']['status']
        risk_level = analysis['sprint_health']['risk_level']
        
        if commitment_status == 'SEVERELY_OVERCOMMITTED':
            analysis['sprint_health']['recommendations'].append(
                "URGENT: Sprint is severely overcommitted - immediately move non-critical work to next sprint"
            )
            analysis['sprint_health']['recommendations'].append(
                "Focus only on highest priority items to prevent complete sprint failure"
            )
        elif commitment_status == 'OVERCOMMITTED':
            analysis['sprint_health']['recommendations'].append(
                "Sprint is overcommitted - consider reducing scope or moving lower priority items"
            )
            analysis['sprint_health']['recommendations'].append(
                "Identify blockers and focus team effort on completing in-progress work"
            )
        elif commitment_status == 'AT_RISK':
            analysis['sprint_health']['recommendations'].append(
                "Sprint delivery is at risk - monitor daily and remove any blockers immediately"
            )
            analysis['sprint_health']['recommendations'].append(
                "Consider deferring any new work that comes up during the sprint"
            )
        else:
            analysis['sprint_health']['recommendations'].append(
                "Sprint is on track - maintain current pace and delivery quality"
            )
            
        # Add specific recommendations based on work distribution
        if work_remaining > work_in_progress and commitment_status != 'ON_TRACK':
            metric_name = "story points" if uses_story_points else "issues"
            analysis['sprint_health']['recommendations'].append(
                f"High risk: {work_remaining} {metric_name} not yet started vs {work_in_progress} in progress"
            )
        
        return '```json\n' + json.dumps(analysis, indent=2) + '\n```'
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze sprint commitment: {e}")

@mcp.tool()
def search_users(query: str, max_results: int = 10) -> str:
    """Search users by query."""
    try:
        users = jira_client.search_users(query, maxResults=max_results)
        return to_markdown([u.raw for u in users])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to search users: {e}")

@mcp.tool()
def list_projects() -> str:
    """List all projects."""
    try:
        projects = jira_client.projects()
        return to_markdown([p.raw for p in projects])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch projects: {e}")

@mcp.tool()
def get_project(project_key: str) -> str:
    """Get a project by key."""
    try:
        project = jira_client.project(project_key)
        return to_markdown(project)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch project: {e}")

@mcp.tool()
def get_project_components(project_key: str) -> str:
    """Get components for a project."""
    try:
        components = jira_client.project_components(project_key)
        return to_markdown([c.raw for c in components])
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch components: {e}")

@mcp.tool()
def get_project_versions(project_key: str) -> str:
    """Get versions for a project."""
    try:
        versions = jira_client.project_versions(project_key)
        return to_markdown([v.raw for v in versions])
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch versions: {e}")

@mcp.tool()
def get_project_roles(project_key: str) -> str:
    """Get roles for a project."""
    try:
        roles = jira_client.project_roles(project_key)
        return to_markdown(roles)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch roles: {e}")

@mcp.tool()
def get_project_permission_scheme(project_key: str) -> str:
    """Get permission scheme for a project."""
    try:
        scheme = jira_client.project_permissionscheme(project_key)
        return to_markdown(scheme.raw)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch permission scheme: {e}")

@mcp.tool()
def get_project_issue_types(project_key: str) -> str:
    """Get issue types for a project."""
    try:
        types = jira_client.project_issue_types(project_key)
        return to_markdown([t.raw for t in types])
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch issue types: {e}")

@mcp.tool()
def get_current_user() -> str:
    """Get current user info."""
    try:
        user = jira_client.myself()
        return to_markdown(user)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch current user: {e}")

@mcp.tool()
def get_user(account_id: str) -> str:
    """Get user by account ID."""
    try:
        user = jira_client.user(account_id)
        return to_markdown(user.raw)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch user: {e}")

@mcp.tool()
def get_assignable_users_for_project(project_key: str, query: str = "", max_results: int = 10) -> str:
    """Get assignable users for a project."""
    try:
        users = jira_client.search_assignable_users_for_projects(query, project_key, maxResults=max_results)
        return to_markdown([u.raw for u in users])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get assignable users: {e}")

@mcp.tool()
def get_assignable_users_for_issue(issue_key: str, query: str = "", max_results: int = 10) -> str:
    """Get assignable users for an issue."""
    try:
        users = jira_client.search_assignable_users_for_issues(query, issueKey=issue_key, maxResults=max_results)
        return to_markdown([u.raw for u in users])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get assignable users: {e}")

@mcp.tool()
def list_boards(max_results: int = 10) -> str:
    """List boards."""
    try:
        boards = jira_client.boards(maxResults=max_results)
        return to_markdown([b.raw for b in boards])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch boards: {e}")

@mcp.tool()
def get_board(board_id: int) -> str:
    """Get board by ID."""
    try:
        board = jira_client.board(board_id)
        return to_markdown(board.raw)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch board: {e}")

@mcp.tool()
def list_sprints(board_id: int, max_results: int = 10) -> str:
    """List sprints for a board."""
    try:
        sprints = jira_client.sprints(board_id, maxResults=max_results)
        return to_markdown([s.raw for s in sprints])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch sprints: {e}")

@mcp.tool()
def get_sprint(sprint_id: int) -> str:
    """Get sprint by ID."""
    try:
        sprint = jira_client.sprint(sprint_id)
        return to_markdown(sprint.raw)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to fetch sprint: {e}")

@mcp.tool()
def get_issues_for_board(board_id: int, max_results: int = 10) -> str:
    """Get issues for a board."""
    try:
        issues = jira_client.get_issues_for_board(board_id, maxResults=max_results)
        return to_markdown([i.raw for i in issues])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch issues for board: {e}")

@mcp.tool()
def get_issues_for_sprint(board_id: int, sprint_id: int, max_results: int = 10) -> str:
    """Get issues for a sprint in a board."""
    try:
        issues = jira_client.get_all_issues_for_sprint_in_board(board_id, sprint_id, maxResults=max_results)
        return to_markdown([i.raw for i in issues])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch issues for sprint: {e}")

@mcp.tool()
def analyze_velocity_trend(board_id: int, sprint_count: int = 3) -> str:
    """Analyze team velocity over the past N sprints to identify trends."""
    try:
        # Get recent sprints for the board
        sprints = jira_client.sprints(board_id, maxResults=50, state='closed,active')
        
        # Sort sprints by start date (most recent first)
        sorted_sprints = sorted([s for s in sprints if hasattr(s, 'startDate') and s.startDate], 
                              key=lambda x: x.startDate, reverse=True)
        
        # Take the specified number of sprints
        target_sprints = sorted_sprints[:sprint_count]
        
        velocity_data = []
        
        for sprint in target_sprints:
            # Get all issues for this sprint
            jql = f"sprint = {sprint.id}"
            fields = 'summary,status,customfield_10016,assignee,priority,issuetype'
            issues = jira_client.search_issues(jql, maxResults=200, fields=fields)
            
            # Calculate metrics for this sprint
            total_committed_points = 0
            completed_points = 0
            total_issues = len(issues)
            completed_issues = 0
            issues_with_points = 0
            
            for issue in issues:
                story_points = getattr(issue.fields, 'customfield_10016', 0) or 0
                status_category = issue.fields.status.statusCategory.name
                
                if story_points:
                    issues_with_points += 1
                    total_committed_points += story_points
                    
                    if status_category in ['Done', 'Complete']:
                        completed_points += story_points
                
                if status_category in ['Done', 'Complete']:
                    completed_issues += 1
            
            sprint_data = {
                'sprint_id': sprint.id,
                'sprint_name': sprint.name,
                'sprint_state': sprint.state,
                'start_date': str(sprint.startDate) if hasattr(sprint, 'startDate') else None,
                'end_date': str(sprint.endDate) if hasattr(sprint, 'endDate') else None,
                'uses_story_points': issues_with_points > 0,
                'story_points': {
                    'committed': total_committed_points,
                    'completed': completed_points,
                    'velocity': completed_points
                },
                'issue_throughput': {
                    'committed': total_issues,
                    'completed': completed_issues,
                    'velocity': completed_issues
                }
            }
            velocity_data.append(sprint_data)
        
        # Calculate trend analysis
        if len(velocity_data) >= 2:
            uses_points = any(s['uses_story_points'] for s in velocity_data)
            
            if uses_points:
                velocities = [s['story_points']['velocity'] for s in velocity_data if s['uses_story_points']]
            else:
                velocities = [s['issue_throughput']['velocity'] for s in velocity_data]
            
            # Calculate trend (simple linear trend)
            if len(velocities) >= 2:
                recent_avg = sum(velocities[:2]) / 2 if len(velocities) >= 2 else velocities[0]
                older_avg = sum(velocities[-2:]) / 2 if len(velocities) >= 2 else velocities[-1]
                trend_direction = "UP" if recent_avg > older_avg else "DOWN" if recent_avg < older_avg else "STABLE"
                trend_percentage = ((recent_avg - older_avg) / older_avg * 100) if older_avg > 0 else 0
            else:
                trend_direction = "INSUFFICIENT_DATA"
                trend_percentage = 0
                
            average_velocity = sum(velocities) / len(velocities) if velocities else 0
        else:
            trend_direction = "INSUFFICIENT_DATA"
            trend_percentage = 0
            average_velocity = 0
        
        analysis = {
            'board_id': board_id,
            'analysis_period': f"Last {sprint_count} sprints",
            'sprints_analyzed': len(velocity_data),
            'velocity_trend': {
                'direction': trend_direction,
                'percentage_change': round(trend_percentage, 1),
                'average_velocity': round(average_velocity, 1),
                'uses_story_points': uses_points,
                'metric_type': 'story_points' if uses_points else 'issue_count'
            },
            'sprint_details': velocity_data,
            'recommendations': []
        }
        
        # Add recommendations based on trend
        if trend_direction == "DOWN":
            analysis['recommendations'].append("Velocity is declining - investigate potential blockers or capacity issues")
            analysis['recommendations'].append("Consider retrospective to identify improvement opportunities")
        elif trend_direction == "UP":
            analysis['recommendations'].append("Velocity is improving - good trend, maintain current practices")
        elif trend_direction == "STABLE":
            analysis['recommendations'].append("Velocity is stable - predictable delivery capacity")
        else:
            analysis['recommendations'].append("Need more completed sprints for trend analysis")
        
        return '```json\n' + json.dumps(analysis, indent=2) + '\n```'
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze velocity trend: {e}")

@mcp.tool()
def analyze_priority_focus(board_id: int, sprint_id: int) -> str:
    """Analyze whether the team is working on the right priority items in the current sprint."""
    try:
        # Get sprint details
        sprint = jira_client.sprint(sprint_id)
        
        # Get all issues in the sprint
        jql = f"sprint = {sprint_id}"
        fields = 'summary,status,customfield_10016,assignee,priority,issuetype,created'
        sprint_issues = jira_client.search_issues(jql, maxResults=200, fields=fields)
        
        # Get all issues in the same project/board that are not in sprint but could be high priority
        if sprint_issues:
            project_key = sprint_issues[0].fields.project.key
            backlog_jql = f"project = {project_key} AND sprint is EMPTY AND resolution is EMPTY"
            backlog_issues = jira_client.search_issues(backlog_jql, maxResults=100, fields=fields)
        else:
            backlog_issues = []
        
        # Analyze sprint issues by priority
        sprint_analysis = defaultdict(lambda: {
            'total': 0, 'in_progress': 0, 'completed': 0, 'todo': 0,
            'total_points': 0, 'completed_points': 0, 'issues': []
        })
        
        for issue in sprint_issues:
            priority = issue.fields.priority.name if issue.fields.priority else 'No Priority'
            status_category = issue.fields.status.statusCategory.name
            story_points = getattr(issue.fields, 'customfield_10016', 0) or 0
            
            sprint_analysis[priority]['total'] += 1
            sprint_analysis[priority]['total_points'] += story_points
            
            issue_info = {
                'key': issue.key,
                'summary': issue.fields.summary,
                'status': issue.fields.status.name,
                'status_category': status_category,
                'assignee': issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned',
                'story_points': story_points
            }
            sprint_analysis[priority]['issues'].append(issue_info)
            
            if status_category in ['Done', 'Complete']:
                sprint_analysis[priority]['completed'] += 1
                sprint_analysis[priority]['completed_points'] += story_points
            elif status_category in ['In Progress', 'In Development']:
                sprint_analysis[priority]['in_progress'] += 1
            else:
                sprint_analysis[priority]['todo'] += 1
        
        # Analyze backlog for missed high-priority items
        backlog_analysis = defaultdict(lambda: {'total': 0, 'issues': []})
        
        for issue in backlog_issues:
            priority = issue.fields.priority.name if issue.fields.priority else 'No Priority'
            backlog_analysis[priority]['total'] += 1
            
            issue_info = {
                'key': issue.key,
                'summary': issue.fields.summary,
                'status': issue.fields.status.name,
                'assignee': issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned',
                'story_points': getattr(issue.fields, 'customfield_10016', 0) or 0,
                'created': str(issue.fields.created)
            }
            backlog_analysis[priority]['issues'].append(issue_info)
        
        # Priority ranking (customize based on your Jira setup)
        priority_order = ['Critical', 'Highest', 'High', 'Medium', 'Low', 'Lowest', 'No Priority']
        
        # Calculate metrics
        critical_in_sprint = sprint_analysis.get('Critical', {}).get('total', 0)
        critical_in_backlog = backlog_analysis.get('Critical', {}).get('total', 0)
        critical_completed = sprint_analysis.get('Critical', {}).get('completed', 0)
        critical_in_progress = sprint_analysis.get('Critical', {}).get('in_progress', 0)
        
        high_priority_in_sprint = (sprint_analysis.get('Critical', {}).get('total', 0) + 
                                 sprint_analysis.get('Highest', {}).get('total', 0) + 
                                 sprint_analysis.get('High', {}).get('total', 0))
        
        total_sprint_issues = sum(data['total'] for data in sprint_analysis.values())
        high_priority_percentage = (high_priority_in_sprint / total_sprint_issues * 100) if total_sprint_issues > 0 else 0
        
        analysis = {
            'sprint_info': {
                'id': sprint_id,
                'name': sprint.name,
                'state': sprint.state
            },
            'priority_focus_analysis': {
                'critical_issues': {
                    'in_sprint_total': critical_in_sprint,
                    'in_sprint_completed': critical_completed,
                    'in_sprint_in_progress': critical_in_progress,
                    'in_backlog_total': critical_in_backlog,
                    'completion_rate': round((critical_completed / critical_in_sprint * 100) if critical_in_sprint > 0 else 0, 1)
                },
                'high_priority_focus': {
                    'high_priority_issues_in_sprint': high_priority_in_sprint,
                    'total_sprint_issues': total_sprint_issues,
                    'high_priority_percentage': round(high_priority_percentage, 1)
                }
            },
            'sprint_breakdown_by_priority': dict(sprint_analysis),
            'backlog_high_priority_items': {
                priority: data for priority, data in backlog_analysis.items() 
                if priority in ['Critical', 'Highest', 'High'] and data['total'] > 0
            },
            'priority_gap_analysis': {
                'focus_score': 'HIGH' if high_priority_percentage >= 60 else 'MEDIUM' if high_priority_percentage >= 40 else 'LOW',
                'critical_coverage': 'GOOD' if critical_in_sprint >= critical_in_backlog else 'NEEDS_ATTENTION',
                'missed_critical_items': critical_in_backlog
            },
            'recommendations': []
        }
        
        # Generate recommendations
        if critical_in_backlog > 0:
            analysis['recommendations'].append(f"Consider adding {critical_in_backlog} critical items from backlog to current or next sprint")
        
        if high_priority_percentage < 50:
            analysis['recommendations'].append("Sprint has low focus on high-priority items - consider reprioritizing")
        
        if critical_in_sprint > 0 and critical_completed == 0 and critical_in_progress == 0:
            analysis['recommendations'].append("Critical items in sprint are not being actively worked on")
        
        if high_priority_percentage >= 70:
            analysis['recommendations'].append("Good focus on high-priority items")
        
        return '```json\n' + json.dumps(analysis, indent=2) + '\n```'
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze priority focus: {e}")

@mcp.tool()
def analyze_team_workload(board_id: int, sprint_id: int) -> str:
    """Analyze team member workload distribution and identify overloaded members."""
    try:
        # Get sprint details
        sprint = jira_client.sprint(sprint_id)
        
        # Get all issues in the sprint
        jql = f"sprint = {sprint_id}"
        fields = 'summary,status,customfield_10016,assignee,priority,issuetype'
        issues = jira_client.search_issues(jql, maxResults=200, fields=fields)
        
        # Analyze workload by assignee
        workload_analysis = defaultdict(lambda: {
            'total_issues': 0,
            'story_points': 0,
            'in_progress_count': 0,
            'completed_count': 0,
            'todo_count': 0,
            'priority_breakdown': defaultdict(int),
            'issues': [],
            'wip_violations': [],
            'load_level': 'NORMAL'
        })
        
        for issue in issues:
            assignee = issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned'
            story_points = getattr(issue.fields, 'customfield_10016', 0) or 0
            status_category = issue.fields.status.statusCategory.name
            priority = issue.fields.priority.name if issue.fields.priority else 'No Priority'
            
            workload_analysis[assignee]['total_issues'] += 1
            workload_analysis[assignee]['story_points'] += story_points
            workload_analysis[assignee]['priority_breakdown'][priority] += 1
            
            issue_info = {
                'key': issue.key,
                'summary': issue.fields.summary,
                'status': issue.fields.status.name,
                'status_category': status_category,
                'story_points': story_points,
                'priority': priority
            }
            workload_analysis[assignee]['issues'].append(issue_info)
            
            # Count by status
            if status_category in ['Done', 'Complete']:
                workload_analysis[assignee]['completed_count'] += 1
            elif status_category in ['In Progress', 'In Development']:
                workload_analysis[assignee]['in_progress_count'] += 1
                # Track for WIP violations (multiple items in progress)
                if workload_analysis[assignee]['in_progress_count'] > 1:
                    workload_analysis[assignee]['wip_violations'].append(issue_info)
            else:
                workload_analysis[assignee]['todo_count'] += 1
        
        # Determine load levels and risks
        for assignee, data in workload_analysis.items():
            total_issues = data['total_issues']
            in_progress = data['in_progress_count']
            
            # Determine load level based on issue count
            if total_issues >= 13:
                data['load_level'] = 'OVERLOADED'
            elif total_issues >= 9:
                data['load_level'] = 'HIGH'
            elif total_issues >= 5:
                data['load_level'] = 'NORMAL'
            else:
                data['load_level'] = 'LIGHT'
            
            # Add risk factors
            data['risk_factors'] = []
            
            if in_progress > 2:
                data['risk_factors'].append(f"WIP violation: {in_progress} items in progress simultaneously")
            
            if total_issues >= 13:
                data['risk_factors'].append("Excessive issue count may lead to burnout")
            
            critical_count = data['priority_breakdown'].get('Critical', 0)
            high_count = data['priority_breakdown'].get('High', 0) + data['priority_breakdown'].get('Highest', 0)
            
            if critical_count + high_count > total_issues * 0.7:
                data['risk_factors'].append("High context-switching risk due to many high-priority items")
        
        # Generate team-level insights
        overloaded_members = [name for name, data in workload_analysis.items() 
                            if data['load_level'] == 'OVERLOADED' and name != 'Unassigned']
        high_load_members = [name for name, data in workload_analysis.items() 
                           if data['load_level'] == 'HIGH' and name != 'Unassigned']
        wip_violators = [name for name, data in workload_analysis.items() 
                        if len(data['wip_violations']) > 0 and name != 'Unassigned']
        
        # Calculate distribution statistics
        assigned_members = [data for name, data in workload_analysis.items() if name != 'Unassigned']
        if assigned_members:
            avg_issues = sum(data['total_issues'] for data in assigned_members) / len(assigned_members)
            max_issues = max(data['total_issues'] for data in assigned_members)
            min_issues = min(data['total_issues'] for data in assigned_members)
        else:
            avg_issues = max_issues = min_issues = 0
        
        analysis = {
            'sprint_info': {
                'id': sprint_id,
                'name': sprint.name,
                'state': sprint.state
            },
            'team_workload_summary': {
                'total_team_members': len([name for name in workload_analysis.keys() if name != 'Unassigned']),
                'overloaded_members': len(overloaded_members),
                'high_load_members': len(high_load_members),
                'wip_violators': len(wip_violators),
                'unassigned_issues': workload_analysis.get('Unassigned', {}).get('total_issues', 0)
            },
            'distribution_statistics': {
                'average_issues_per_person': round(avg_issues, 1),
                'max_issues_per_person': max_issues,
                'min_issues_per_person': min_issues,
                'distribution_balance': 'UNBALANCED' if max_issues - min_issues > 8 else 'BALANCED'
            },
            'individual_workloads': dict(workload_analysis),
            'immediate_risks': {
                'overloaded_members': overloaded_members,
                'high_load_members': high_load_members,
                'wip_violators': wip_violators,
                'sprint_failure_risk': 'HIGH' if overloaded_members else 'MEDIUM' if high_load_members or wip_violators else 'LOW'
            },
            'recommendations': []
        }
        
        # Generate specific recommendations
        if overloaded_members:
            analysis['recommendations'].append(f"URGENT: Redistribute work from overloaded members: {', '.join(overloaded_members)}")
        
        if wip_violators:
            analysis['recommendations'].append(f"Address WIP violations for: {', '.join(wip_violators)} - focus on completing current work")
        
        if workload_analysis.get('Unassigned', {}).get('total_issues', 0) > 0:
            analysis['recommendations'].append("Assign unassigned issues to prevent bottlenecks")
        
        if max_issues - min_issues > 8:
            analysis['recommendations'].append("Rebalance workload distribution across team members")
        
        if not overloaded_members and not wip_violators:
            analysis['recommendations'].append("Workload distribution looks healthy - monitor progress")
        
        return '```json\n' + json.dumps(analysis, indent=2) + '\n```'
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to analyze team workload: {e}")

@mcp.tool()
def sprint_health_dashboard(board_id: int, sprint_id: int) -> str:
    """Comprehensive sprint health analysis combining commitment, priority focus, and workload distribution."""
    try:
        # Get basic sprint info first
        sprint = jira_client.sprint(sprint_id)
        
        # Get all issues in the sprint
        jql = f"sprint = {sprint_id}"
        fields = 'summary,status,customfield_10016,assignee,priority,issuetype,created'
        issues = jira_client.search_issues(jql, maxResults=200, fields=fields)
        
        if not issues:
            return '```json\n' + json.dumps({
                'error': f'No issues found in sprint {sprint_id}',
                'sprint_info': {
                    'id': sprint_id,
                    'name': sprint.name,
                    'state': sprint.state
                }
            }, indent=2) + '\n```'
        
        # 1. COMMITMENT ANALYSIS
        total_committed_points = 0
        completed_points = 0
        in_progress_points = 0
        total_issues = len(issues)
        completed_issues = 0
        in_progress_issues = 0
        issues_with_points = 0
        
        # 2. PRIORITY ANALYSIS
        priority_breakdown = defaultdict(lambda: {'total': 0, 'completed': 0, 'in_progress': 0})
        
        # 3. WORKLOAD ANALYSIS
        workload_by_assignee = defaultdict(lambda: {
            'total_issues': 0, 'in_progress_count': 0, 'completed_count': 0,
            'story_points': 0, 'high_priority_count': 0
        })
        
        # Process each issue
        for issue in issues:
            story_points = getattr(issue.fields, 'customfield_10016', 0) or 0
            status_category = issue.fields.status.statusCategory.name
            priority = issue.fields.priority.name if issue.fields.priority else 'No Priority'
            assignee = issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned'
            
            # Commitment metrics
            if story_points:
                issues_with_points += 1
                total_committed_points += story_points
                if status_category in ['Done', 'Complete']:
                    completed_points += story_points
                elif status_category in ['In Progress', 'In Development']:
                    in_progress_points += story_points
            
            if status_category in ['Done', 'Complete']:
                completed_issues += 1
            elif status_category in ['In Progress', 'In Development']:
                in_progress_issues += 1
            
            # Priority metrics
            priority_breakdown[priority]['total'] += 1
            if status_category in ['Done', 'Complete']:
                priority_breakdown[priority]['completed'] += 1
            elif status_category in ['In Progress', 'In Development']:
                priority_breakdown[priority]['in_progress'] += 1
            
            # Workload metrics
            workload_by_assignee[assignee]['total_issues'] += 1
            workload_by_assignee[assignee]['story_points'] += story_points
            
            if status_category in ['Done', 'Complete']:
                workload_by_assignee[assignee]['completed_count'] += 1
            elif status_category in ['In Progress', 'In Development']:
                workload_by_assignee[assignee]['in_progress_count'] += 1
            
            if priority in ['Critical', 'Highest', 'High']:
                workload_by_assignee[assignee]['high_priority_count'] += 1
        
        # Calculate key metrics
        uses_story_points = issues_with_points > 0
        if uses_story_points:
            completion_rate = (completed_points / total_committed_points * 100) if total_committed_points > 0 else 0
        else:
            completion_rate = (completed_issues / total_issues * 100) if total_issues > 0 else 0
        
        # Commitment status
        if completion_rate >= 80:
            commitment_status = 'ON_TRACK'
        elif completion_rate >= 60:
            commitment_status = 'AT_RISK'
        elif completion_rate >= 40:
            commitment_status = 'OVERCOMMITTED'
        else:
            commitment_status = 'SEVERELY_OVERCOMMITTED'
        
        # Priority focus analysis
        critical_total = priority_breakdown.get('Critical', {}).get('total', 0)
        critical_in_progress = priority_breakdown.get('Critical', {}).get('in_progress', 0)
        high_priority_total = (priority_breakdown.get('Critical', {}).get('total', 0) +
                             priority_breakdown.get('Highest', {}).get('total', 0) +
                             priority_breakdown.get('High', {}).get('total', 0))
        high_priority_percentage = (high_priority_total / total_issues * 100) if total_issues > 0 else 0
        
        # Team workload analysis
        assigned_members = [name for name in workload_by_assignee.keys() if name != 'Unassigned']
        overloaded_members = []
        wip_violators = []
        
        for assignee, data in workload_by_assignee.items():
            if assignee != 'Unassigned':
                if data['total_issues'] >= 13:
                    overloaded_members.append(assignee)
                if data['in_progress_count'] > 2:
                    wip_violators.append(assignee)
        
        # Overall health assessment
        health_risks = []
        overall_risk = 'LOW'
        
        if commitment_status in ['OVERCOMMITTED', 'SEVERELY_OVERCOMMITTED']:
            health_risks.append('Sprint overcommitted')
            overall_risk = 'HIGH'
        
        if critical_total > 0 and critical_in_progress == 0:
            health_risks.append('Critical items not being worked on')
            overall_risk = 'HIGH' if overall_risk != 'HIGH' else overall_risk
        
        if overloaded_members:
            health_risks.append(f'{len(overloaded_members)} team members overloaded')
            overall_risk = 'HIGH' if overall_risk != 'HIGH' else overall_risk
        
        if wip_violators:
            health_risks.append(f'{len(wip_violators)} WIP limit violations')
            overall_risk = 'MEDIUM' if overall_risk == 'LOW' else overall_risk
        
        if high_priority_percentage < 50:
            health_risks.append('Low focus on high-priority items')
            overall_risk = 'MEDIUM' if overall_risk == 'LOW' else overall_risk
        
        if not health_risks:
            health_risks.append('No major risks identified')
        
        # Generate recommendations
        recommendations = []
        
        if commitment_status == 'SEVERELY_OVERCOMMITTED':
            recommendations.append("🚨 CRITICAL: Immediately reduce sprint scope - move non-essential work to backlog")
        elif commitment_status == 'OVERCOMMITTED':
            recommendations.append("⚠️  Consider reducing sprint scope or extending timeline")
        
        if overloaded_members:
            recommendations.append(f"👥 Redistribute work from overloaded members: {', '.join(overloaded_members)}")
        
        if wip_violators:
            recommendations.append(f"🔄 Address WIP violations: {', '.join(wip_violators)} should focus on completing current work")
        
        if critical_total > 0 and critical_in_progress == 0:
            recommendations.append(f"🔥 Start working on {critical_total} critical items immediately")
        
        if high_priority_percentage < 50:
            recommendations.append("📋 Increase focus on high-priority items in this sprint")
        
        if not recommendations:
            recommendations.append("✅ Sprint health looks good - maintain current approach")
        
        dashboard = {
            'sprint_info': {
                'id': sprint_id,
                'name': sprint.name,
                'state': sprint.state,
                'board_id': board_id
            },
            'overall_health': {
                'risk_level': overall_risk,
                'commitment_status': commitment_status,
                'completion_rate': round(completion_rate, 1),
                'health_risks': health_risks
            },
            'commitment_summary': {
                'is_overcommitted': commitment_status in ['OVERCOMMITTED', 'SEVERELY_OVERCOMMITTED'],
                'uses_story_points': uses_story_points,
                'total_work': total_committed_points if uses_story_points else total_issues,
                'completed_work': completed_points if uses_story_points else completed_issues,
                'in_progress_work': in_progress_points if uses_story_points else in_progress_issues,
                'completion_rate': round(completion_rate, 1)
            },
            'priority_summary': {
                'critical_issues_total': critical_total,
                'critical_issues_in_progress': critical_in_progress,
                'high_priority_percentage': round(high_priority_percentage, 1),
                'priority_focus_score': 'HIGH' if high_priority_percentage >= 60 else 'MEDIUM' if high_priority_percentage >= 40 else 'LOW'
            },
            'team_summary': {
                'total_team_members': len(assigned_members),
                'overloaded_members': len(overloaded_members),
                'wip_violators': len(wip_violators),
                'unassigned_issues': workload_by_assignee.get('Unassigned', {}).get('total_issues', 0),
                'team_capacity_risk': 'HIGH' if overloaded_members else 'MEDIUM' if wip_violators else 'LOW'
            },
            'key_recommendations': recommendations,
            'detailed_breakdowns': {
                'priority_breakdown': dict(priority_breakdown),
                'workload_breakdown': dict(workload_by_assignee)
            }
        }
        
        return '```json\n' + json.dumps(dashboard, indent=2) + '\n```'
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate sprint health dashboard: {e}")

# ─── 5. Run the HTTP-based MCP server on port 8000 ───────────────────────────────
if __name__ == "__main__":
    mcp.run()
