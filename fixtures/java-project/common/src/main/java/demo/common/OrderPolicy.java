package demo.common;
public final class OrderPolicy {
    public static void requireTenant(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) throw new SecurityException("TENANT_REQUIRED");
    }
    public static void requireLimit(int limit) {
        if (limit < 1 || limit > 5000) throw new IllegalArgumentException("EXPORT_LIMIT_OUT_OF_RANGE");
    }
}
