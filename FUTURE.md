# TODO's and FEATURES:
[] - Improve on autonomus browser user (i.e. passing human verification, captcha, etc)
[] - CLI: COnfiguration Manager (maybe first TUI usecase): ensure API is extensible for web
[] - TOOLS: Create a disable tools list (by profile) to improve context management and other issues 
[] - GATEWAY: Separate Background Model Config?
[] - PERMISSIONS | AUTH : properly fix internal task tracker permissions. update to better delineate between risk, effect class, authorization (i.e. read_only, none, ambient vs mutating, ricky_state, explicit.)
[] - GENERAL: Tasks, and similar things need "human readable name" + the task ID
[] - STATE STORES: session / task database purging (i.e. clear out or archive completed, processes, finished tansactions after set times)
[] - MODELS / AGENT: Make Ricky Multi Modal (i.e. image / video / audio inputs)
	* Partly complete for browser... need to integrate w/ gateway, cli, jobs, etc
[] - DOCUMENTATION: Create interactive architecture diagram & audit / explore current state architecture
[] - GATEWAY: Redesign gateway configuration around profiles
	* Profile specific job message / notification routing
	* Agent configs by profile (i.e. separate capabilities, models, or even fully separate gateway agents)
[] - MODELS / AGENT: Provider Caching (are we effectively using this?)
[] - MODELS / AGENT: Context Mangement- 3 low lift improvments to context management and context efficiency

# ACTIVE DOGFOOD USECASES:
[] - Calendar Management
[] - Scheduled "Jobs"
	* Email / Slack Triage
	* Daily Breifings
[] - Gateway Interactions
[] - Memory
	* Tune memory classifiers / criteria



# GENERAL IDEAS
* Logging / Verbosity- 
	* Gateway background logging
	* Agent look basic, verbose, debug logging
* Config management (manage and update configuration via cli/tui)
* Context Management
	* Continue improving context management
	* Review tools / skill loading. Can this be more efficient / dynamic
* Trace Dump- Add / command to dump session debug trace out for analysis or showing planner agent examples of issues


# FUTURE TOOLS / CAPABILITIES
* Browser Control
* Computer Control 


# ROADMAP
1) Nice TUI
2) HTTP + Web Interface (Include PWA?)
