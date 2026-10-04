package demo.order;

public class OrderPolicy {
    public static void requireRead(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) throw new SecurityException("tenant required");
    }
}
