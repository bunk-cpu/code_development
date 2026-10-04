package demo.broken;
public class LegacyBridge {
    public void send() { MissingAuditClient.send(); }
}
