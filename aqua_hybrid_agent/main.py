from config.settings import Settings
from aqua.client import AquaClient

def main():
    cfg = Settings()
    client = AquaClient(cfg.aqua_base_url, cfg.aqua_team_code)
    client.login()
    print("AQuA authenticated. DRY_RUN=", cfg.dry_run)
    print("Order limits:", client.order_limit())
    # TODO: market universe -> technical features -> news -> Gemini JSON decision
    # -> live account reconciliation -> risk checks -> execution -> persistent audit log

if __name__ == "__main__":
    main()
