package demo.orders;
import java.util.List;
import demo.common.OrderPolicy;
import org.springframework.stereotype.Service;
import org.springframework.context.annotation.Profile;
@Service
@Profile("prod")
public class ExportService implements ExportPort {
    private final OrderMapper mapper;
    public ExportService(OrderMapper mapper) { this.mapper = mapper; }
    @Override public List<String> create(String tenantId, int limit) {
        OrderPolicy.requireTenant(tenantId);
        OrderPolicy.requireLimit(limit);
        return mapper.findPaid(tenantId, limit);
    }
    public List<String> create(String tenantId) { return create(tenantId, 100); }
}
