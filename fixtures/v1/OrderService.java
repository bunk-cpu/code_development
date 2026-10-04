package demo.order;

public class OrderService {
    public String summary(String tenantId, String status) {
        return "tenant=" + tenantId + ";status=" + status;
    }
}
